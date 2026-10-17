// Audit entries are stored under short machine names. People read the trail, so the interface
// shows a sentence and keeps the raw name for anyone grepping the file.

const LABELS: Record<string, string> = {
  "container.restart": "Container restarted",
  "file.download": "File downloaded",
  "file.upload": "File uploaded",
  "file.preview": "File previewed",
  "logs.download": "Log bundle saved",
  "logs.search": "Logs searched",
  "tunnel.up": "Tunnel opened",
  "tunnel.down": "Tunnel closed",
  "tunnel.dropped": "Tunnel dropped",
  "tunnel.reopen": "Tunnel reopened",
  "ssh.open": "Shell opened",
  "web.open": "Browser let in",
  "mux.reset": "Connections reset",
  "mux.stop": "Connections closed",
  "env.add": "Environment registered",
  "env.remove": "Environment removed",
  "settings.env.add": "Environment registered",
  "settings.env.update": "Environment changed",
  "settings.env.remove": "Environment removed",
  "settings.host.add": "Server added",
  "settings.host.update": "Server changed",
  "settings.host.remove": "Server removed",
  "settings.tunnel.add": "Tunnel defined",
  "settings.tunnel.update": "Tunnel changed",
  "settings.tunnel.remove": "Tunnel deleted",
  "settings.refresh": "Check interval changed",
  "settings.thresholds": "Alert levels changed",
  "settings.project.add": "Project created",
  "settings.project.update": "Project changed",
  "settings.project.rename": "Project renamed",
  "settings.project.remove": "Project removed",
  "config.restore": "Configuration restored",
  "config.import": "Bundle imported",
  "config.export": "Bundle exported",
  "audit.export": "Audit trail exported",
};

/** Colour by what the action touches, so the column scans at a glance. */
const COLORS: Record<string, string> = {
  container: "orange",
  file: "blue",
  logs: "cyan",
  tunnel: "grape",
  ssh: "teal",
  mux: "gray",
  env: "yellow",
  settings: "yellow",
  config: "grape",
  audit: "gray",
};

export function actionLabel(action: string): string {
  if (LABELS[action]) return LABELS[action];
  // Unknown action: turn "thing.was_done" into "Thing was done" rather than showing a code.
  const words = action.replace(/[._]/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

export function actionColor(action: string): string {
  return COLORS[action.split(".")[0]] ?? "gray";
}
