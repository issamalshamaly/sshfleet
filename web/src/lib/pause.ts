import type { PausedBy } from "../api/types";

interface Pausable {
  alias: string;
  paused_by: PausedBy | null;
}

/** Paused on its own account, so resuming it is a matter for this host alone. */
export function pausedItself(host: Pausable): boolean {
  return host.paused_by?.kind === "host" && host.paused_by.name === host.alias;
}

/** "acme-prod-api", "Environment staging", "Project acme": how a pause target is named. An
 *  environment is held by its id; given the environments, it is called by its name instead. */
export function pauseTargetLabel(target: PausedBy, environments?: { id: string; name: string }[]): string {
  if (target.kind === "host") return target.name;
  if (target.kind === "project") return `Project ${target.name}`;
  return `Environment ${environments?.find((e) => e.id === target.name)?.name ?? target.name}`;
}

/** One sentence on why sshfleet makes no connection to this host. */
export function pauseReason(host: Pausable): string {
  const by = host.paused_by;
  if (!by) return "";
  if (by.kind !== "host") return `${pauseTargetLabel(by)} is paused — sshfleet makes no connection to any of its hosts`;
  if (by.name === host.alias) return "Monitoring paused — sshfleet makes no connection to this host";
  return `Reached through ${by.name}, whose monitoring is paused`;
}
