import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "src") },
  },
  server: {
    port: 5173,
    host: "127.0.0.1",
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/health": { target: "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
  preview: { port: 4173, host: "127.0.0.1" },
  build: {
    chunkSizeWarningLimit: 900,
    rolldownOptions: {
      output: {
        advancedChunks: {
          groups: [
            { name: "echarts", test: /node_modules[\\/]echarts/ },
            { name: "react", test: /node_modules[\\/](react|react-dom|react-router|scheduler)/ },
          ],
        },
      },
    },
  },
});
