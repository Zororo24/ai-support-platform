import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Overridden to http://api:8000 inside Compose.
const api = process.env.API_PROXY_TARGET ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/health": api,
      "/auth": api,
      "/chat": api,
      "/conversations": api,
      "/tickets": api,
      "/orders": api,
      "/admin": api,
    },
  },
});
