import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The Career Scout web app (T067). A second entry beside the legacy dashboard so it
// reuses the same node_modules and stylesheet; it builds straight into the
// Python package, which `career-scout serve` serves and the wheel ships.
//
// base is "/" (not "./" like the legacy build): client routes are nested
// (/opportunities/abc), and a relative asset path would resolve under them.
export default defineConfig({
  root: "app",
  base: "/",
  plugins: [react()],
  server: {
    port: 5174,
    // `career-scout serve` must be running. Host stays localhost:5174, which the
    // API's Host check accepts.
    proxy: { "/api": "http://127.0.0.1:8765" },
  },
  build: { outDir: "../../src/career_scout/web/static", emptyOutDir: true, sourcemap: false },
});
