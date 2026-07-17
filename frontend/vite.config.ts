import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The API's dev origin. In dev we proxy /api to the FastAPI server so the
// browser makes same-origin requests and there is no CORS to configure.
const API_TARGET = process.env.VITE_API_TARGET ?? "http://127.0.0.1:7860";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: API_TARGET,
        changeOrigin: true,
      },
    },
  },
});
