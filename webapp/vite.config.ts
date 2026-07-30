import { defineConfig } from "vite";

export default defineConfig({
  build: {
    outDir: "dist",
    // Telegram aggressively caches Mini App assets; hashed filenames are what makes
    // a redeploy actually reach the user rather than serving yesterday's bundle.
    assetsDir: "assets",
    sourcemap: true,
  },
  server: {
    // `npm run dev` alone cannot talk to the API: initData only exists inside the
    // Telegram client. Point this at a tunnel when developing against a real bot.
    proxy: {
      "/webapp": {
        target: process.env.VITE_API_ORIGIN ?? "http://127.0.0.1:8100",
        changeOrigin: true,
      },
    },
  },
});
