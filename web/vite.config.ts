import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

/** The running `sshfleet web` only hands its session token to its own page. This dev page is not
 *  that page, so the proxy adds the token itself, read from the file `sshfleet web` keeps it in
 *  (0600, this user's) — the same way `sshfleet web` run a second time asks it for a link. */
function sessionToken(): string {
  const env = process.env;
  const state = env.SSHFLEET_HOME
    ? join(env.SSHFLEET_HOME, "state")
    : join(env.XDG_STATE_HOME || join(homedir(), ".local", "state"), "sshfleet");
  try {
    return JSON.parse(readFileSync(join(state, "web-session.json"), "utf8")).token ?? "";
  } catch {
    return "";
  }
}

// The build lands inside the Python package so `sshfleet web` serves it with no extra step.
export default defineConfig(({ command }) => {
  const token = command === "serve" ? sessionToken() : "";
  const target = process.env.SSHFLEET_DEV_TARGET ?? "127.0.0.1:8765";
  return {
    plugins: [react()],
    build: {
      outDir: "../src/sshfleet/web/static",
      emptyOutDir: true,
      sourcemap: false,
      chunkSizeWarningLimit: 900,
    },
    server: {
      port: 5173,
      proxy: {
        "/api": { target: `http://${target}`, changeOrigin: false, headers: { "x-sshfleet-token": token } },
        "/ws": {
          target: `ws://${target}`,
          ws: true,
          rewrite: (path: string) => path.replace(/([?&])token=[^&]*/, `$1token=${encodeURIComponent(token)}`),
        },
      },
    },
  };
});
