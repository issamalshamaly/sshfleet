import { useEffect, useMemo } from "react";
import { useLocation } from "react-router-dom";

/**
 * Links into the Logs page.
 *
 * The page keeps what you left on it — the sources, the pattern, the results, your place in
 * them — so that walking off to the fleet and back, by the sidebar or the browser's Back
 * button, costs you nothing. A click on a Logs icon *beside a host or a container* is the one
 * exception: naming one is a new question, not a return to the old one, so that link carries
 * this marker and the page starts from scratch.
 */
export const FRESH_LOGS = { state: { freshLogs: true } } as const;

/** Did this navigation come from a Logs icon beside a host or a container? */
export function isFreshLogs(state: unknown): boolean {
  return (state as { freshLogs?: boolean } | null)?.freshLogs === true;
}

// The marker rides on the history entry, so it is still there when Back or Forward lands on
// that entry again. That is a return, not the question being asked a second time: each entry
// counts as fresh once, the first time it is reached.
const asked = new Set<string>();

/** True on the visit a Logs icon opened, false on every later landing on the same history entry. */
export function useFreshLogs(): boolean {
  const location = useLocation();
  const fresh = useMemo(() => isFreshLogs(location.state) && !asked.has(location.key), [location.key, location.state]);
  useEffect(() => {
    if (fresh) asked.add(location.key);
  }, [fresh, location.key]);
  return fresh;
}

export function logsPath(opts: { env?: string; host?: string; container?: string; pattern?: string } = {}): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(opts)) if (value) params.set(key, value);
  const query = params.toString();
  return query ? `/logs?${query}` : "/logs";
}
