import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Requests go through this proxy, so the API needs no CORS configuration.
const apiPort = process.env.API_PORT ?? "8000";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": {
        target: `http://127.0.0.1:${apiPort}`,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});
