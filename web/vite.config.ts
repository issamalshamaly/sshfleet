import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The build lands inside the Python package so `sshfleet web` serves it with no extra step.
export default defineConfig({
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
      "/api": { target: "http://127.0.0.1:8765", changeOrigin: false },
      "/ws": { target: "ws://127.0.0.1:8765", ws: true },
    },
  },
});
