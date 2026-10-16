// Shapes returned by the sshfleet API. Kept in one place so a backend change is one edit here.

export type Severity = "crit" | "warn" | "info";

export interface Environment {
  name: string;
  short: string;
  color: string;
  project: string;
  hosts: number;
  bastions: string[];
  production: boolean;
  /** Monitoring stopped for every host in it (its own flag; its project's is on the project). */
  paused: boolean;
}

/** A named grouping of environments, and the outermost thing sshfleet knows about. Every
 *  environment belongs to exactly one. Its one behaviour is pausing: nothing about how a host
 *  is reached comes from here. */
export interface Project {
  name: string;
  color: string;
  description: string;
  /** Monitoring stopped for every host of every environment in it. */
  paused: boolean;
}

/** What keeps sshfleet from connecting to a host: a paused host (itself, or a jump host on its
 *  way), its paused environment, or its paused project — the widest one when several are. */
export interface PausedBy {
  kind: "host" | "environment" | "project";
  name: string;
}

export interface ProjectSettings extends Project {
  environments: string[];
  hosts: number;
}

export interface Alert {
  code: string;
  severity: Severity;
  message: string;
  host: string;
  container: string | null;
  key: string;
}

export interface Disk {
  mount: string;
  size: number;
  used: number;
  pct: number;
}

export interface Container {
  name: string;
  image: string;
  state: string;
  status: string;
  restart_count: number;
  exit_code: number | null;
  network_mode: string;
  ports: string;
  log_bytes: number | null;
  /** Matches one of the host's ignore globs: shown, but never alerted on. */
  ignored: boolean;
}

/** One offer in a container's quick-actions menu, built by the server for that container. */
export interface QuickAction {
  id: string;
  title: string;
  detail: string;
  /** "shell" · "toolkit" · "watch" — how the menu groups them. */
  group: string;
  /** It changes something inside the container. Drawn apart from the read-only ones. */
  writes: boolean;
  /** The exact remote line, shown before it runs. */
  command: string;
}

export interface Probe {
  port: number;
  path: string;
  code: string;
  ok: boolean;
  at: number; // when this probe ran; older than the snapshot when it was carried forward
}

/** A domain this host serves, read out of a web server's own configuration. */
export interface Site {
  name: string;
  port: number;
  ssl: boolean;
  source: string;
  url: string;
}

export interface Snapshot {
  alias: string;
  env: string;
  at: number;
  reachable: boolean;
  error: { kind: string | null; message: string } | null;
  hostname: string;
  kernel: string;
  uptime_s: number;
  load: [number, number, number];
  cpus: number;
  mem: { total: number; available: number; used_pct: number };
  disks: Disk[];
  runtime: string;
  sudo_ok: boolean;
  containers: Container[];
  missing_containers: string[];
  vanished_containers: string[];
  expected_count: number | null;
  listening: number[];
  probes: Probe[];
  frontends: { name: string; mtime: number }[];
  nginx: string;
  sites: Site[];
  alerts: { code: string; severity: Severity; message: string; container: string | null }[];
  elapsed: number;
}

export interface Host {
  alias: string;
  env: string;
  label: string;
  hostname: string | null;
  user: string | null;
  via: string | null;
  is_bastion: boolean;
  main: boolean;
  role: string;
  runtime: string | null;
  sudo: boolean;
  tags: string[];
  workloads: string | null;
  expected_count: number | null;
  ignored_containers: string[];
  health_port: number | null;
  health_path: string | null;
  snapshot: Snapshot | null;
  stale: boolean;
  age_s: number | null;
  /** Its ssh master is open. */
  connected: boolean;
  /** Monitoring is stopped, and what stopped it. */
  paused_by: PausedBy | null;
}

export interface Fleet {
  hosts: Host[];
  refreshing: boolean;
  last_refresh: number;
  bastions: Record<string, boolean>;
  mux_alive: number;
  mux_total: number;
  alerts: Alert[];
}

export interface SoundSettings {
  enabled: boolean;
  crit: string;
  warn: string;
  done: string;
  desktop: boolean;
}

export interface AppConfig {
  version: string;
  projects: Project[];
  environments: Environment[];
  thresholds: Record<string, number>;
  refresh: Record<string, number>;
  logs: {
    default_before: number;
    default_after: number;
    default_window: string;
    tail_lines: number;
    max_bytes: number;
    display_tz: string;
  };
  ui: { sounds: SoundSettings; [key: string]: unknown };
  download_dir: string;
  warnings: string[];
  started_at: number;
  /** The code on disk is newer than the process answering: the page and the routes disagree. */
  stale: boolean;
}

export interface KeyInfo {
  path: string;
  exists: boolean;
  ok: boolean;
  mode: string | null;
  problems: string[];
  fix: string | null;
}

export interface EnvironmentSettings {
  name: string;
  short: string;
  color: string;
  ssh_config_path: string;
  ssh_config_exists: boolean;
  key: KeyInfo | null;
  alias_pattern: string;
  alias_prefix: string | null;
  /** `<project>_<short>_`: every host added to this environment starts with it. */
  host_prefix: string;
  bastions: string[];
  bastions_auto: boolean;
  hosts: number;
  production: boolean;
  project: string;
  /** Its own pause flag; its project's is on the project. */
  paused: boolean;
}

export interface RefreshSettings {
  fast_s: number;
  stats_s: number;
  stale_after_s: number;
  collector_timeout_s: number;
  connect_timeout_s: number;
}

export interface Settings {
  projects: ProjectSettings[];
  environments: EnvironmentSettings[];
  refresh: RefreshSettings;
  thresholds: Record<string, number>;
  paths: { config_file: string; ssh_config: string; download_dir: string; audit_file: string; backups: string };
  watching: string[];
  last_reload: number;
  last_reload_reason: string;
  warnings: string[];
}

export interface HostSettings {
  alias: string;
  env: string;
  hostname: string | null;
  user: string | null;
  port: number | null;
  proxy_jump: string | null;
  is_bastion: boolean;
  main: boolean;
  label: string;
  role: string;
  workloads: string | null;
  log_format: string;
  runtime: string | null;
  sudo: boolean;
  tags: string[];
  display: string | null;
  source: string | null;
  editable: boolean;
  override: boolean;
  health_port: number | null;
  health_path: string | null;
  probe_min_interval_s: number;
  expected_count: number | null;
  ignored_containers: string[];
  /** Seconds this host is given to connect; null means the fleet-wide budget. */
  connect_timeout_s: number | null;
  /** Monitoring stopped on this host itself. */
  paused: boolean;
  /** What keeps this one out of reach, when something does. */
  paused_by: PausedBy | null;
}

export interface SshConfigCandidate {
  path: string;
  name: string;
  registered_to: string | null;
  hosts: number;
  sample?: string[];
  error?: string;
}

export interface SshInclude {
  config: string;
  exists: boolean;
  includes_config_d: boolean;
  line: string;
}

export interface EnvironmentPreview {
  path: string;
  new_file: boolean;
  hosts: string[];
  summary: { hosts: number; users: Record<string, number>; without_identity_file: number; proxy_jumps: Record<string, number> };
  prefix: string | null;
  bastions: string[];
  key_candidates: (KeyInfo | null)[];
  suggested: { name: string; short: string; color: string };
}

export interface ReloadSummary {
  ok: boolean;
  reason?: string;
  added?: string[];
  removed?: string[];
  changed?: string[];
  hosts?: number;
  error?: string;
  /** The history state this reload left the configuration in, and the one before it. */
  state?: string | null;
  previous_state?: string | null;
}

/** Who caused a state: this page, an editor (noticed by the server or by a CLI run), a command,
 *  the server starting, a restore or an import. */
export type HistorySource = "web" | "watcher" | "disk" | "cli" | "start" | "restore" | "import";

export interface HistoryFile {
  role: "config" | "ssh";
  path: string;
  env: string | null;
  size: number;
  mode: number;
  missing: boolean;
  /** Why the content was not kept: too large, looks like a key, holds key material. */
  skipped: string | null;
  kept: boolean;
}

export interface HistoryChanges {
  first?: boolean;
  files?: { path: string; role: string; change: string }[];
  hosts_added?: string[];
  hosts_removed?: string[];
  config?: string[];
}

export interface HistoryState {
  id: string;
  /** The six characters typed to confirm a restore. */
  code: string;
  at: string;
  reason: string;
  source: HistorySource;
  valid: boolean;
  error: string | null;
  restored_from: string | null;
  sshfleet_version: string;
  config_version: number | null;
  files: HistoryFile[];
  changes: HistoryChanges;
}

export interface HistoryPage {
  states: HistoryState[];
  total: number;
  /** A restore or an import is writing the configuration right now. */
  busy: boolean;
}

export interface FileDiff {
  path: string;
  role: string;
  change: "changed" | "same" | "added" | "removed" | "unavailable";
  diff: string;
  note: string | null;
}

export interface RestorePlanSummary {
  target: string;
  writes: string[];
  unchanged: string[];
  left_in_place: string[];
  unavailable: { path: string; why: string }[];
  migrated: boolean;
}

export interface HistoryDetail {
  state: HistoryState;
  against: "previous" | "current";
  previous: string | null;
  diffs: FileDiff[];
  restore?: RestorePlanSummary | { refused: string };
}

export interface ImportChange {
  kind: "project" | "environment" | "stanza" | "host" | "tunnel" | "setting";
  name: string;
  action: "add" | "update" | "same" | "kept-local" | "conflict" | "remove" | "skipped";
  note: string;
}

export interface ImportPlanSummary {
  mode: "merge" | "replace";
  prefer: "local" | "bundle";
  changed: boolean;
  config_changed: boolean;
  changes: ImportChange[];
  warnings: string[];
  /** Environments brought in without a key on this machine. */
  needs_key: string[];
  /** Settings that arrived as <redacted> and have no value here. */
  needs_value: string[];
  files: { path: string; new: boolean; diff: string }[];
  /** Sent back to apply exactly this plan. */
  digest: string;
}

export interface BundleAbout {
  created_at: string | null;
  created_on: string | null;
  sshfleet_version: string | null;
  scope: { projects: string[]; environments: string[] } | null;
  with_local: boolean | null;
}

export interface ImportOptions {
  mode: "merge" | "replace";
  prefer: "local" | "bundle";
  withLocal: boolean;
  allowCommands: boolean;
}

export interface ImportDryRun {
  bundle: BundleAbout;
  plan: ImportPlanSummary;
}

export interface ImportResult {
  bundle: BundleAbout;
  ok: boolean;
  written: string[];
  before: string | null;
  after: string | null;
  plan: ImportPlanSummary;
  reload: ReloadSummary | null;
}

export interface RestoreResult {
  ok: boolean;
  target: string;
  before: string | null;
  after: string | null;
  written: string[];
  unchanged: string[];
  left_in_place: string[];
  unavailable: { path: string; why: string }[];
  migrated: boolean;
  reload: ReloadSummary | null;
}

export interface ConnectionTest {
  ok: boolean;
  elapsed?: number;
  bastions?: Record<string, { ok: boolean; error: string; seconds: number }>;
  host?: { alias: string; ok: boolean; error: string; seconds: number };
  alias?: string;
  error?: string;
  seconds?: number;
}

export type FleetEvent =
  | { type: "hello"; clients: number }
  | { type: "ping" }
  | { type: "host"; host: Host }
  | { type: "progress"; done: number; total: number }
  | { type: "refresh_started"; at: number }
  | { type: "refresh_done"; at: number; bastions: Record<string, boolean>; mux_alive: number }
  | { type: "alert"; state: "appeared" | "cleared"; alert: Alert }
  | { type: "job_done"; job: string; ok: boolean }
  | { type: "inventory"; reason: string; added: string[]; removed: string[]; changed: string[]; hosts: number; at: number }
  | { type: "tunnel"; state: "dropped" | "retrying" | "reopened" | "closed"; name: string; tunnel: Tunnel | null }
  | { type: "error"; message: string };

export interface RestartPreview {
  host: string;
  container: string;
  env: string;
  production: boolean;
  command: string;
  preview: string;
  current: { state: string; status: string; restarts: number } | null;
}

export interface JobStep {
  type: "step";
  phase: string;
  message: string;
  elapsed: number;
  state: string | null;
  probe: string | null;
}

export interface JobResult {
  type: "result";
  ok: boolean;
  phase: string;
  message: string;
  elapsed?: number;
  state?: string | null;
  probe?: string | null;
  logs?: string;
}

export interface FileEntry {
  name: string;
  kind: "d" | "f" | "l";
  size: number;
  mtime: number;
  mode: string;
  owner: string;
  group: string;
  link: string;
  denied: string | null;
}

export interface Listing {
  host: string;
  path: string;
  used_sudo: boolean;
  truncated: boolean;
  entries: FileEntry[];
}

export interface Tunnel {
  name: string;
  env: string;
  via: string;
  local: number;
  remote: string;
  endpoint: string;
  url: string | null;
  /** Something answers on the local port, and it is this tunnel. */
  up: boolean;
  /** Opened and not closed since. Stays true while a dropped tunnel is being reopened. */
  wanted: boolean;
  /** Wanted, but nothing listens any more: its connection died. */
  dropped: boolean;
  /** Closed, yet something still listens on the local port. */
  held: boolean;
  since: number | null;
  error: string;
  /** Failed reopen attempts since it dropped, and when the next one is due (epoch seconds). */
  attempts: number;
  retry_at: number | null;
}

export interface LogSourceInfo {
  id: string;
  kind: string;
  env: string;
  label: string;
  host: string | null;
  service: string | null;
  container: string | null;
  path: string | null;
  format: string;
  deploy_env: string | null;
}

export interface LogSourcesResponse {
  main: LogSourceInfo[];
  hosts: {
    alias: string;
    label: string;
    main: boolean;
    role: string;
    containers: { name: string; state: string; watched: boolean }[];
    files: LogSourceInfo[];
  }[];
}

export interface LogSelection {
  main?: boolean;
  starred_hosts?: string[];
  hosts?: string[];
  containers?: { host: string; name: string }[];
  /** `id` as /api/logs/sources gave it; `path` only for a file nobody listed. */
  files?: { host: string; id?: string; path?: string }[];
  include_files?: boolean;
}

export interface LogSearchBody {
  env: string;
  selection: LogSelection;
  pattern: string;
  mode: "regex" | "fixed";
  ignore_case: boolean;
  before: number;
  after: number;
  window: string;
  tz?: string;
  tail_lines?: number;
  max_bytes?: number;
  download?: boolean;
  raw_dump?: boolean;
}

export interface LogLine {
  type: "line";
  source: string;
  label: string;
  ts: number | null; // epoch ms
  match: boolean;
  text: string;
  entry: number;
  cont: boolean;
  level: string | null;
  group: number;
}

export type LogEvent =
  | { type: "start"; sources: LogSourceInfo[] }
  | LogLine
  | { type: "progress"; source: string; lines: number; bytes: number; matches: number }
  | {
      type: "source_done";
      source: string;
      label: string;
      lines: number;
      matches: number;
      bytes: number;
      elapsed: number;
      truncated: boolean;
      truncated_reason: string;
      coverage_ok: boolean;
    }
  | { type: "source_error"; source: string; label: string; kind: string; message: string }
  | {
      type: "summary";
      lines: number;
      matches: number;
      bytes: number;
      elapsed: number;
      since: string;
      until: string;
      truncated: string[];
      failed: string[];
      bundle: string | null;
      bundle_name: string | null;
    };

export interface Bundle {
  name: string;
  env: string;
  pattern: string;
  created_at: string;
  totals: { lines: number; matches: number; bytes: number };
  window: { since_utc: string; until_utc: string; spec: string };
  size: number;
  files: string[];
}

export interface AuditEntry {
  ts: string;
  actor: string;
  action: string;
  env?: string;
  host?: string;
  target?: string;
  command?: string;
  status?: string;
  note?: string;
  duration_ms?: number;
  verify?: Record<string, unknown>;
}

export interface AuditPage {
  entries: AuditEntry[];
  total: number;
}
