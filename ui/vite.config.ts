import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Served by the gateway under /dashboard (see src/relay/api/app.py); `npm run dev` proxies
// the API to a local gateway so the same relative URLs work in development.
export default defineConfig({
  base: "/dashboard/",
  plugins: [react()],
  // Recharts is most of the bundle (~175 KB gzipped in total): fine for an admin dashboard.
  build: { chunkSizeWarningLimit: 700 },
  server: {
    proxy: {
      "/admin": process.env.RELAY_URL ?? "http://localhost:8080",
      "/v1": process.env.RELAY_URL ?? "http://localhost:8080",
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
  },
});
