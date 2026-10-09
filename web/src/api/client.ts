// Thin fetch wrapper. Every request carries the per-session token the page was served with.

import type {
  AppConfig,
  AuditPage,
  Bundle,
  ConnectionTest,
  EnvironmentPreview,
  Fleet,
  Host,
  HostSettings,
  ProjectSettings,
  KeyInfo,
  QuickAction,
  ReloadSummary,
  Listing,
  LogSearchBody,
  LogSelection,
  LogSourcesResponse,
  RefreshSettings,
  RestartPreview,
  Settings,
  SshConfigCandidate,
  Tunnel,
  SoundSettings,
  SshInclude,
} from "./types";

export function sessionToken(): string {
  const meta = document.querySelector('meta[name="sshfleet-token"]') as HTMLMetaElement | null;
  return meta?.content ?? "";
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("x-sshfleet-token", sessionToken());
  if (init.body && !(init.body instanceof FormData) && !headers.has("content-type")) {
    headers.set("content-type", "application/json");
  }
  const response = await fetch(path, { ...init, headers });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = body.detail ?? JSON.stringify(body);
    } catch {
      /* not JSON */
    }
    throw new ApiError(response.status, detail);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  config: () => request<AppConfig>("/api/config"),
  fleet: (env?: string) => request<Fleet>(`/api/fleet${env ? `?env=${encodeURIComponent(env)}` : ""}`),
  host: (alias: string) => request<Host>(`/api/hosts/${encodeURIComponent(alias)}`),
  refresh: (opts: { env?: string; project?: string; alias?: string } = {}) => {
    const params = new URLSearchParams();
    if (opts.env) params.set("env", opts.env);
    if (opts.project) params.set("project", opts.project);
    if (opts.alias) params.set("alias", opts.alias);
    return request<{ ok: boolean }>(`/api/fleet/refresh?${params}`, { method: "POST" });
  },
  restartPreview: (alias: string, name: string) =>
    request<RestartPreview>(`/api/hosts/${encodeURIComponent(alias)}/containers/${encodeURIComponent(name)}/preview`),
  restart: (alias: string, name: string, confirm: string) =>
    request<{ job: string }>(`/api/hosts/${encodeURIComponent(alias)}/containers/${encodeURIComponent(name)}/restart`, {
      method: "POST",
      body: JSON.stringify({ confirm }),
    }),
  removeContainer: (alias: string, name: string, confirm: string, saveLogs: boolean) =>
    request<{ ok: boolean; state: string; message: string; command: string; saved_log: string | null }>(
      `/api/hosts/${encodeURIComponent(alias)}/containers/${encodeURIComponent(name)}?confirm=${encodeURIComponent(confirm)}&save_logs=${saveLogs ? "1" : "0"}`,
      { method: "DELETE" },
    ),
  containerActions: (alias: string, name: string) =>
    request<{ host: string; container: string; actions: QuickAction[] }>(
      `/api/hosts/${encodeURIComponent(alias)}/containers/${encodeURIComponent(name)}/actions`,
    ),
  /** Stop alerting on a container. "family" saves a glob covering every generated name like it. */
  ignoreContainer: (alias: string, name: string, scope: "name" | "family" = "name") =>
    request<{ ok: boolean; pattern: string; ignored: string[] }>(
      `/api/hosts/${encodeURIComponent(alias)}/containers/${encodeURIComponent(name)}/ignore?scope=${scope}`,
      { method: "POST" },
    ),
  files: {
    ls: (alias: string, path: string, hidden: boolean) =>
      request<Listing>(
        `/api/files/ls?alias=${encodeURIComponent(alias)}&path=${encodeURIComponent(path)}&hidden=${hidden}`,
      ),
    preview: async (alias: string, path: string) => {
      const response = await fetch(
        `/api/files/preview?alias=${encodeURIComponent(alias)}&path=${encodeURIComponent(path)}`,
        { headers: { "x-sshfleet-token": sessionToken() } },
      );
      if (!response.ok) throw new ApiError(response.status, (await response.json()).detail ?? response.statusText);
      return response.text();
    },
    downloadUrl: (alias: string, path: string) =>
      `/api/files/download?alias=${encodeURIComponent(alias)}&path=${encodeURIComponent(path)}&token=${encodeURIComponent(sessionToken())}`,
    upload: (alias: string, directory: string, file: File) => {
      const form = new FormData();
      form.set("alias", alias);
      form.set("directory", directory);
      form.set("file", file);
      return request<{ ok: boolean; bytes: number; method: string; destination: string }>("/api/files/upload", {
        method: "POST",
        body: form,
      });
    },
  },
  tunnels: {
    list: () => request<Tunnel[]>("/api/tunnels"),
    up: (name: string) => request<Tunnel>(`/api/tunnels/${encodeURIComponent(name)}/up`, { method: "POST" }),
    down: (name: string) => request<Tunnel>(`/api/tunnels/${encodeURIComponent(name)}/down`, { method: "POST" }),
  },
  logs: {
    sources: (env: string, host?: string) =>
      request<LogSourcesResponse>(
        `/api/logs/sources?env=${encodeURIComponent(env)}${host ? `&host=${encodeURIComponent(host)}` : ""}`,
      ),
    bundles: () => request<Bundle[]>("/api/logs/bundles"),
    bundleZipUrl: (name: string) => `/api/logs/bundles/${encodeURIComponent(name)}.zip?token=${encodeURIComponent(sessionToken())}`,
  },
  audit: (limit = 200) => request<AuditPage>(`/api/audit?limit=${limit}`),
  settings: {
    get: () => request<Settings>("/api/settings"),
    hosts: () => request<HostSettings[]>("/api/settings/hosts"),
    projects: () => request<ProjectSettings[]>("/api/settings/projects"),
    addProject: (body: Record<string, unknown>) =>
      request<{ ok: true; project: ProjectSettings }>("/api/settings/projects", { method: "POST", body: JSON.stringify(body) }),
    updateProject: (name: string, body: Record<string, unknown>) =>
      request<{ ok: true; project: ProjectSettings }>(`/api/settings/projects/${encodeURIComponent(name)}`, {
        method: "PATCH",
        body: JSON.stringify(body),
      }),
    removeProject: (name: string, confirm: string) =>
      request<{ ok: true; detached: string[] }>(
        `/api/settings/projects/${encodeURIComponent(name)}?confirm=${encodeURIComponent(confirm)}`,
        { method: "DELETE" },
      ),
    sshConfigs: () => request<SshConfigCandidate[]>("/api/settings/ssh-configs"),
    keys: () => request<KeyInfo[]>("/api/settings/keys"),
    /** An empty `path` previews the file sshfleet would create: config.d/<project>_<short>. */
    previewEnvironment: (path: string, draft: { name?: string; short?: string; project?: string | null } = {}) =>
      request<EnvironmentPreview>("/api/settings/environments/preview", {
        method: "POST",
        body: JSON.stringify({
          ssh_config_path: path,
          name: draft.name ?? null,
          short: draft.short ?? null,
          project: draft.project ?? null,
        }),
      }),
    addEnvironment: (body: Record<string, unknown>) =>
      request<{ ok: boolean; created_file: string | null; reload: ReloadSummary; ssh_include: SshInclude }>("/api/settings/environments", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    sshInclude: () => request<SshInclude>("/api/settings/ssh-include"),
    addSshInclude: () => request<SshInclude & { ok: boolean; backup: string | null }>("/api/settings/ssh-include", { method: "POST" }),
    updateEnvironment: (name: string, body: Record<string, unknown>) =>
      request<{ ok: boolean; reload: ReloadSummary }>(`/api/settings/environments/${encodeURIComponent(name)}`, {
        method: "PATCH",
        body: JSON.stringify(body),
      }),
    removeEnvironment: (name: string) =>
      request<{ ok: boolean; reload: ReloadSummary }>(`/api/settings/environments/${encodeURIComponent(name)}`, {
        method: "DELETE",
      }),
    testEnvironment: (name: string) =>
      request<ConnectionTest>(`/api/settings/environments/${encodeURIComponent(name)}/test`, { method: "POST" }),
    addHost: (body: Record<string, unknown>) =>
      request<{ ok: boolean; alias: string; file: string; backup: string; reload: ReloadSummary }>("/api/settings/hosts", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    updateHost: (alias: string, body: Record<string, unknown>) =>
      request<{ ok: boolean; alias: string; backup: string | null; reload: ReloadSummary }>(
        `/api/settings/hosts/${encodeURIComponent(alias)}`,
        { method: "PATCH", body: JSON.stringify(body) },
      ),
    removeHost: (alias: string, confirm: string) =>
      request<{ ok: boolean; backup: string | null; reload: ReloadSummary }>(
        `/api/settings/hosts/${encodeURIComponent(alias)}?confirm=${encodeURIComponent(confirm)}`,
        { method: "DELETE" },
      ),
    testHost: (alias: string) =>
      request<ConnectionTest>(`/api/settings/hosts/${encodeURIComponent(alias)}/test`, { method: "POST" }),
    setRefresh: (body: Partial<RefreshSettings> & { fast_s: number }) =>
      request<{ ok: boolean; refresh: RefreshSettings }>("/api/settings/refresh", { method: "PUT", body: JSON.stringify(body) }),
    setThresholds: (body: Record<string, number>) =>
      request<{ ok: boolean }>("/api/settings/thresholds", { method: "PUT", body: JSON.stringify(body) }),
    reload: () => request<ReloadSummary>("/api/settings/reload", { method: "POST" }),
    setSounds: (body: Partial<SoundSettings>) =>
      request<{ ok: boolean; sounds: SoundSettings }>("/api/settings/sounds", { method: "PUT", body: JSON.stringify(body) }),
    addTunnel: (body: Record<string, unknown>) =>
      request<{ ok: boolean; tunnel: string }>("/api/settings/tunnels", { method: "POST", body: JSON.stringify(body) }),
    updateTunnel: (name: string, body: Record<string, unknown>) =>
      request<{ ok: boolean; tunnel: string }>(`/api/settings/tunnels/${encodeURIComponent(name)}`, {
        method: "PATCH",
        body: JSON.stringify(body),
      }),
    removeTunnel: (name: string) =>
      request<{ ok: boolean }>(`/api/settings/tunnels/${encodeURIComponent(name)}`, { method: "DELETE" }),
  },
};

/**
 * Read a newline-delimited JSON stream, calling `onEvent` for each object.
 * Resolves when the stream ends; rejects on HTTP errors. Abort with the controller.
 */
export async function streamNdjson<T>(
  path: string,
  init: RequestInit,
  onEvent: (event: T) => void,
  signal?: AbortSignal,
): Promise<void> {
  const headers = new Headers(init.headers);
  headers.set("x-sshfleet-token", sessionToken());
  if (init.body && !headers.has("content-type")) headers.set("content-type", "application/json");
  const response = await fetch(path, { ...init, headers, signal });
  if (!response.ok || !response.body) {
    let detail = response.statusText;
    try {
      detail = (await response.json()).detail ?? detail;
    } catch {
      /* ignore */
    }
    throw new ApiError(response.status, detail);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let index: number;
    while ((index = buffer.indexOf("\n")) !== -1) {
      const line = buffer.slice(0, index);
      buffer = buffer.slice(index + 1);
      if (!line.trim()) continue;
      try {
        onEvent(JSON.parse(line) as T);
      } catch {
        /* skip a malformed line rather than kill the stream */
      }
    }
  }
  if (buffer.trim()) {
    try {
      onEvent(JSON.parse(buffer) as T);
    } catch {
      /* ignore */
    }
  }
}

export function logSearchStream(body: LogSearchBody, onEvent: (e: import("./types").LogEvent) => void, signal: AbortSignal) {
  return streamNdjson("/api/logs/search", { method: "POST", body: JSON.stringify(body) }, onEvent, signal);
}

export function logFollowStream(
  body: { env: string; selection: LogSelection; pattern: string; since: string },
  onEvent: (e: import("./types").LogEvent) => void,
  signal: AbortSignal,
) {
  return streamNdjson("/api/logs/follow", { method: "POST", body: JSON.stringify(body) }, onEvent, signal);
}

export function jobStream(jobId: string, onEvent: (e: import("./types").JobStep | import("./types").JobResult) => void, signal?: AbortSignal) {
  return streamNdjson(`/api/jobs/${encodeURIComponent(jobId)}/stream`, { method: "GET" }, onEvent, signal);
}

export function terminalSocketUrl(
  alias: string,
  cols: number,
  rows: number,
  // A quick action is named, never sent: the server builds the command from its own catalogue.
  quick?: { action?: string; container?: string },
): string {
  const protocol = location.protocol === "https:" ? "wss" : "ws";
  const params = new URLSearchParams({ token: sessionToken(), cols: String(cols), rows: String(rows) });
  if (quick?.action) {
    params.set("action", quick.action);
    if (quick.container) params.set("container", quick.container);
  }
  return `${protocol}://${location.host}/ws/terminal/${encodeURIComponent(alias)}?${params}`;
}
