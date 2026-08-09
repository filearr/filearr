import { cp, mkdir } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { svelte } from "@sveltejs/vite-plugin-svelte";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig, type Plugin } from "vite";
import { VitePWA } from "vite-plugin-pwa";

// Vite 8: Rolldown-based production builds by default.
//
// AGPL-3.0 §13: the running instance must offer users its Corresponding Source.
// The footer "Source" link points here. Override at build time with
// FILEARR_SOURCE_URL (e.g. a fork/tagged release) — defaults to the canonical
// repository.
const SOURCE_URL =
  process.env.FILEARR_SOURCE_URL ?? "https://github.com/pwsh/filearr";

const HERE = dirname(fileURLToPath(import.meta.url));

// Self-hosted Swagger UI: FastAPI's default docs page loads its JS/CSS from a
// CDN, which is wrong for a self-hosted app. Vendor the two swagger-ui-dist
// assets into dist/ so the backend's /api/docs route can serve them from the
// same origin (backend falls back to the CDN only in dev, where dist/ is
// absent).
function copySwaggerAssets(): Plugin {
  return {
    name: "filearr:copy-swagger-assets",
    apply: "build",
    async closeBundle() {
      const src = resolve(HERE, "node_modules/swagger-ui-dist");
      const out = resolve(HERE, "dist/swagger-ui");
      await mkdir(out, { recursive: true });
      for (const f of ["swagger-ui-bundle.js", "swagger-ui.css"]) {
        await cp(resolve(src, f), resolve(out, f));
      }
    },
  };
}

export default defineConfig({
  define: {
    __SOURCE_URL__: JSON.stringify(SOURCE_URL),
    __APP_VERSION__: JSON.stringify(process.env.FILEARR_VERSION ?? "dev"),
  },
  plugins: [
    svelte(),
    tailwindcss(),
    copySwaggerAssets(),
    VitePWA({
      registerType: "autoUpdate",
      workbox: {
        // The SPA navigation fallback must never swallow server-rendered
        // surfaces: /api/** (REST + /api/docs + openapi.json + agent-dist)
        // and /docs/** (the bundled mkdocs manual). Without this denylist the
        // installed service worker answered /api/docs with the SPA shell.
        navigateFallbackDenylist: [/^\/api(\/|$)/, /^\/docs(\/|$)/],
        // The vendored Swagger assets are copied after the bundle is written;
        // keep them out of the PWA precache (~1.5 MB, only needed on
        // /api/docs which the SW never handles anyway).
        globIgnores: ["swagger-ui/**"],
      },
      manifest: {
        name: "Filearr",
        short_name: "Filearr",
        description: "Unified media catalog & search",
        theme_color: "#0f172a",
        display: "standalone",
        icons: [
          { src: "/icon-192.png", sizes: "192x192", type: "image/png" },
          { src: "/icon-512.png", sizes: "512x512", type: "image/png" },
          {
            src: "/icon-512.png",
            sizes: "512x512",
            type: "image/png",
            purpose: "maskable",
          },
        ],
      },
    }),
  ],
  server: {
    proxy: { "/api": "http://localhost:8000" },
  },
});
