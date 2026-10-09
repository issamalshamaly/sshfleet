import { notifications } from "@mantine/notifications";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "../api/client";
import type { HostSettings, Settings } from "../api/types";

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

/** Stop or resume monitoring of one host. Pausing also closes the connection the server holds. */
export function usePauseHost() {
  const invalidate = useInvalidateSettings();
  return useMutation({
    mutationFn: ({ host, paused }: { host: { alias: string }; paused: boolean }) =>
      api.settings.updateHost(host.alias, { paused }),
    onSuccess: (_, { host, paused }) => {
      invalidate();
      notifications.show({
        title: paused ? `${host.alias} paused` : `${host.alias} resumed`,
        message: paused
          ? "Its connection is closed and sshfleet will not connect to it until you resume it."
          : "Checking it now.",
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
