import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Served by memory-core at /live, with hashed assets under /live/assets.
// `npm run dev` proxies the activity stream to the real server over Tailscale,
// which adds the viewer's Tailscale-User-Login header; add ?demo to fake traffic instead.
export default defineConfig({
  base: "/live/",
  plugins: [react(), tailwindcss()],
  build: { outDir: "dist", assetsDir: "assets" },
  server: {
    proxy: {
      "/api": { target: "https://memory.your-tailnet.ts.net:8420", changeOrigin: true },
    },
  },
});
