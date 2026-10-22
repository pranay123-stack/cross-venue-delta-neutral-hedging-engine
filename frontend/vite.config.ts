import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

// The dev server proxies /api and /ws to the backend so the browser sees a
// single origin and CORS never enters the picture during development.
export default defineConfig({
  plugins: [react()],
  resolve: { alias: { "@": path.resolve(__dirname, "src") } },
  server: {
    port: 5173,
    host: true,
    proxy: {
      "/api": { target: process.env.VITE_API_TARGET ?? "http://localhost:8000", changeOrigin: true },
      "/ws": {
        target: process.env.VITE_API_TARGET ?? "http://localhost:8000",
        ws: true,
        // A socket torn down by a closing tab surfaces as EPIPE/ECONNRESET;
        // that is normal, so only other proxy errors are worth printing.
        configure: (proxy) => {
          proxy.on("error", (err: NodeJS.ErrnoException) => {
            if (err.code !== "EPIPE" && err.code !== "ECONNRESET") console.error("[ws proxy]", err);
          });
        },
      },
    },
  },
  build: { outDir: "dist", sourcemap: true },
});
