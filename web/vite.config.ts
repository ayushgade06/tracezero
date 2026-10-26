/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The browser only talks to this server. /api is forwarded to the TraceZero API, so the API needs no CORS
// and stays on 127.0.0.1. TRACEZERO_API_URL is read here (Node side) and never reaches the bundle.
const api = process.env.TRACEZERO_API_URL ?? "http://127.0.0.1:8000";
const proxy = { "/api": { target: api, rewrite: (p: string) => p.replace(/^\/api/, "") } };

export default defineConfig({
  plugins: [react()],
  server: { host: "127.0.0.1", port: 5173, strictPort: true, proxy },
  preview: { host: "127.0.0.1", port: 4173, strictPort: true, proxy },
  test: { environment: "jsdom" },
});
