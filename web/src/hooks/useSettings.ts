import { notifications } from "@mantine/notifications";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "../api/client";
import type { HostSettings, PausedBy, Settings } from "../api/types";
import { pauseTargetLabel } from "../lib/pause";

export function useSettings() {
  return useQuery<Settings>({ queryKey: ["settings"], queryFn: api.settings.get, staleTime: 15_000 });
}

export function useSettingsHosts() {
  return useQuery<HostSettings[]>({ queryKey: ["settings", "hosts"], queryFn: api.settings.hosts, staleTime: 15_000 });
}

/** After any settings change the fleet, the inventory and this page are all stale. */
export function useInvalidateSettings() {
  const client = useQueryClient();
  return () => {
    void client.invalidateQueries({ queryKey: ["settings"] });
    void client.invalidateQueries({ queryKey: ["fleet"] });
    void client.invalidateQueries({ queryKey: ["config"] });
  };
}

/** Stop or resume monitoring of one host, one environment or a whole project. Pausing also
 *  closes every connection the server holds to the hosts it covers. */
export function usePause() {
  const invalidate = useInvalidateSettings();
  return useMutation({
    mutationFn: async ({ target, paused }: { target: PausedBy; paused: boolean }): Promise<void> => {
      if (target.kind === "project") await api.settings.updateProject(target.name, { paused });
      else if (target.kind === "environment") await api.settings.updateEnvironment(target.name, { paused });
      else await api.settings.updateHost(target.name, { paused });
    },
    onSuccess: (_, { target, paused }) => {
      invalidate();
      const one = target.kind === "host";
      notifications.show({
        title: `${pauseTargetLabel(target)} ${paused ? "paused" : "resumed"}`,
        message: paused
          ? one
            ? "Its connection is closed and sshfleet will not connect to it until you resume it."
            : "Every connection to its hosts is closed and sshfleet will not connect to them until you resume it."
          : one
            ? "Checking it now."
            : "Checking its hosts now.",
        color: paused ? "orange" : "teal",
      });
    },
    onError: (error) => notifications.show({ title: "Could not save", message: String((error as Error).message), color: "red" }),
  });
}

/** The exact lines that will be written into the SSH config file. */
export function renderStanza(host: {
  alias: string;
  hostname?: string | null;
  user?: string | null;
  port?: number | string | null;
  proxyJump?: string | null;
  identityFile?: string | null;
}): string {
  const lines = [`Host ${host.alias || "…"}`];
  if (host.hostname) lines.push(`    HostName ${host.hostname}`);
  if (host.user) lines.push(`    User ${host.user}`);
  if (host.port) lines.push(`    Port ${host.port}`);
  if (host.proxyJump) lines.push(`    ProxyJump ${host.proxyJump}`);
  if (host.identityFile) lines.push(`    IdentityFile ${host.identityFile}`);
  return lines.join("\n");
}
