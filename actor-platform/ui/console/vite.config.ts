import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Built to ../static, which the API serves. There is no frontend server in
// production: the console is static files on the SAME ORIGIN as the API, which
// is what lets the session cookie work without CORS and without exposing the
// API to another origin at all.
export default defineConfig({
  plugins: [react()],
  base: "/console/",
  build: {
    outDir: "../static",
    emptyOutDir: true,
    // Never inline an SVG as a data: URI. The favicon is referenced from
    // index.html, and an inlined SVG carries its xmlns URL into the page --
    // which test_console.py rightly reads as the page referencing another host.
    assetsInlineLimit: (file: string) => (file.endsWith(".svg") ? false : undefined),
  },
  server: {
    // `npm run dev` only. Proxies to a locally running API so the cookie is
    // still same-origin from the browser's point of view.
    proxy: Object.fromEntries(
      ["/auth", "/me", "/admin", "/social", "/actors", "/runs", "/health"].map((p) => [
        p,
        { target: "http://127.0.0.1:8000", changeOrigin: false, ws: true },
      ]),
    ),
  },
});
