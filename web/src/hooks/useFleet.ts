import { notifications } from "@mantine/notifications";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { api, streamNdjson } from "../api/client";
import type { AppConfig, Fleet, FleetEvent, Host } from "../api/types";
import { fmtTime } from "../lib/format";
import { notifyDesktop, play } from "../lib/sounds";

export function useConfig() {
  return useQuery<AppConfig>({ queryKey: ["config"], queryFn: api.config, staleTime: 5 * 60_000 });
}

export function useFleet() {
  // One query for the whole fleet; the env filter is applied in the components so that switching
  // environments is instant and the live stream has a single place to patch.
  return useQuery<Fleet>({ queryKey: ["fleet"], queryFn: () => api.fleet(), staleTime: 60_000 });
}

export interface LiveState {
  connected: boolean;
  progress: { done: number; total: number } | null;
  lastRefresh: number | null;
}

/**
 * Keeps the fleet query patched from the server's event stream and surfaces alert transitions
 * as toasts. Reconnects with a short backoff if the server goes away.
 */
export function useFleetEvents(): LiveState {
  const client = useQueryClient();
  const [live, setLive] = useState<LiveState>({ connected: false, progress: null, lastRefresh: null });
  const stopped = useRef(false);

  useEffect(() => {
    stopped.current = false;
    let controller: AbortController | null = null;
    let backoff = 1000;

    const patchHost = (host: Host) => {
      client.setQueryData<Fleet>(["fleet"], (current) => {
        if (!current) return current;
        const index = current.hosts.findIndex((h) => h.alias === host.alias);
        const hosts = index === -1 ? [...current.hosts, host] : current.hosts.map((h, i) => (i === index ? host : h));
        return { ...current, hosts };
      });
    };

    const onEvent = (event: FleetEvent) => {
      switch (event.type) {
        case "hello":
          setLive((s) => ({ ...s, connected: true }));
          backoff = 1000;
          break;
        case "host":
          patchHost(event.host);
          break;
        case "progress":
          setLive((s) => ({ ...s, progress: event.done >= event.total ? null : { done: event.done, total: event.total } }));
          break;
        case "refresh_started":
          setLive((s) => ({ ...s, progress: { done: 0, total: 0 } }));
          break;
        case "refresh_done":
          setLive((s) => ({ ...s, progress: null, lastRefresh: event.at }));
          client.setQueryData<Fleet>(["fleet"], (current) =>
            current ? { ...current, bastions: event.bastions, mux_alive: event.mux_alive, last_refresh: event.at, refreshing: false } : current,
          );
          void client.invalidateQueries({ queryKey: ["fleet"] });
          break;
        case "alert": {
          if (event.alert.severity !== "info") {
            notifications.show({
              id: `${event.alert.key}:${event.state}`,
              title: event.state === "appeared" ? `${event.alert.code} · ${event.alert.host}` : `cleared · ${event.alert.host}`,
              message: event.alert.message,
              color: event.state === "cleared" ? "teal" : event.alert.severity === "crit" ? "red" : "yellow",
              autoClose: 8000,
            });
          }
          // Sounds follow the saved settings; the config query is the single source so a change
          // on the Settings page applies to the very next event without a reload.
          const sounds = client.getQueryData<AppConfig>(["config"])?.ui?.sounds;
          if (event.state === "appeared" && sounds?.enabled) {
            if (event.alert.severity === "crit") play(sounds.crit);
            else if (event.alert.severity === "warn") play(sounds.warn);
            if (sounds.desktop && event.alert.severity === "crit") {
              notifyDesktop(`${event.alert.code} · ${event.alert.host}`, event.alert.message);
            }
          }
          break;
        }
        case "job_done": {
          void client.invalidateQueries({ queryKey: ["fleet"] });
          const sounds = client.getQueryData<AppConfig>(["config"])?.ui?.sounds;
          if (sounds?.enabled) play(sounds.done);
          break;
        }
        case "inventory": {
          // The SSH config files changed, here or in an editor. Everything derived from them is
          // now stale, and the operator gets told what moved rather than wondering.
          void client.invalidateQueries({ queryKey: ["fleet"] });
          void client.invalidateQueries({ queryKey: ["config"] });
          void client.invalidateQueries({ queryKey: ["settings"] });
          void client.invalidateQueries({ queryKey: ["settings", "hosts"] });
          const parts = [
            event.added.length ? `${event.added.length} added` : "",
            event.removed.length ? `${event.removed.length} removed` : "",
            event.changed.length ? `${event.changed.length} changed` : "",
          ].filter(Boolean);
          if (parts.length) {
            notifications.show({
              title: `Hosts reloaded — ${parts.join(", ")}`,
              message: [...event.added, ...event.removed, ...event.changed].slice(0, 6).join(", "),
              color: "blue",
              autoClose: 6000,
            });
          }
          break;
        }
        case "tunnel": {
          // A tunnel that dropped by itself. Its toast stays until the tunnel is back or closed,
          // so a database client that suddenly lost its connection has the answer on screen.
          void client.invalidateQueries({ queryKey: ["tunnels"] });
          const id = `tunnel-drop:${event.name}`;
          const tunnel = event.tunnel;
          if (event.state === "dropped") {
            notifications.show({
              id,
              title: `${event.name} dropped`,
              message: tunnel
                ? `Nothing answers on ${tunnel.endpoint} any more: the connection to ${tunnel.via} went. Reopening it…`
                : "Reopening it…",
              color: "orange",
              loading: true,
              autoClose: false,
            });
            const sounds = client.getQueryData<AppConfig>(["config"])?.ui?.sounds;
            if (sounds?.enabled) play(sounds.warn);
          } else if (event.state === "retrying") {
            notifications.update({
              id,
              title: `${event.name} dropped`,
              message: `Could not reopen it yet${tunnel?.error ? ` — ${tunnel.error}` : ""}. Next try at ${fmtTime(tunnel?.retry_at)}.`,
              color: "orange",
              loading: true,
              autoClose: false,
            });
          } else if (event.state === "reopened") {
            notifications.hide(id);
            notifications.show({
              title: `${event.name} reopened`,
              message: tunnel
                ? `${tunnel.endpoint} → ${tunnel.remote} via ${tunnel.via}. Connections made before it dropped have to reconnect.`
                : "",
              color: "teal",
              autoClose: 10_000,
            });
          } else {
            notifications.hide(id); // closed by hand: nothing left to report
          }
          break;
        }
        case "error":
          notifications.show({ title: "server", message: event.message, color: "red" });
          break;
        default:
          break;
      }
    };

    const connect = async () => {
      while (!stopped.current) {
        controller = new AbortController();
        try {
          await streamNdjson<FleetEvent>("/api/events", { method: "GET" }, onEvent, controller.signal);
        } catch {
          /* fall through to reconnect */
        }
        if (stopped.current) break;
        setLive((s) => ({ ...s, connected: false }));
        await new Promise((resolve) => setTimeout(resolve, backoff));
        backoff = Math.min(backoff * 2, 15_000);
      }
    };
    void connect();

    return () => {
      stopped.current = true;
      controller?.abort();
    };
  }, [client]);

  return live;
}
