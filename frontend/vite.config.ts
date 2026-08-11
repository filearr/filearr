import { readFileSync } from "node:fs";
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

// --- About page: the frontend half of the stack, baked in at build time -----
//
// The container image ships only `dist/` — there is no package.json, no
// node_modules and no Node runtime beside the served bundle — so the deployed
// app cannot introspect what it was built from. The only moment that knowledge
// exists is right here, during the build, which is why it is injected as a
// constant rather than served from an endpoint.
//
// RESOLVED versions, not declared ranges. package.json says "^5.56.8", which is
// a wish; node_modules/svelte/package.json says which build the bundle actually
// contains, and those differ after any `npm update`. The declared range is only
// the fallback for the (broken-install) case where the package is not present.
type StackEntry = { name: string; version: string; kind: "runtime" | "build"; url: string };

/** Normalise npm's several `repository` spellings into a browsable https URL. */
function repoUrl(repository: unknown): string | null {
  const raw =
    typeof repository === "string"
      ? repository
      : typeof (repository as { url?: unknown })?.url === "string"
        ? ((repository as { url: string }).url)
        : null;
  if (!raw) return null;
  // "git+https://github.com/x/y.git", "git://…", "github:x/y" all occur.
  let u = raw.replace(/^git\+/, "").replace(/\.git$/, "");
  if (u.startsWith("github:")) u = `https://github.com/${u.slice("github:".length)}`;
  if (u.startsWith("git://")) u = `https://${u.slice("git://".length)}`;
  if (u.startsWith("git@github.com:")) u = `https://github.com/${u.slice("git@github.com:".length)}`;
  return u.startsWith("https://") ? u : null;
}

function readFrontendStack(): { node: string; built_at: string; packages: StackEntry[] } {
  const root = JSON.parse(readFileSync(resolve(HERE, "package.json"), "utf8"));
  const groups: [Record<string, string>, StackEntry["kind"]][] = [
    [root.dependencies ?? {}, "runtime"],
    [root.devDependencies ?? {}, "build"],
  ];
  const packages: StackEntry[] = [];
  for (const [deps, kind] of groups) {
    for (const [name, range] of Object.entries(deps)) {
      let version = String(range);
      // Each package's OWN manifest is the authority on its version and its
      // links, exactly as importlib.metadata is on the Python side — so no URL
      // table is hand-maintained here either.
      let url: string | null = null;
      try {
        const own = JSON.parse(
          readFileSync(resolve(HERE, "node_modules", name, "package.json"), "utf8"),
        );
        if (own.version) version = String(own.version);
        url = (typeof own.homepage === "string" ? own.homepage : null) ?? repoUrl(own.repository);
      } catch {
        // Not installed (or an odd layout): keep the declared range, marked as
        // such so the page never presents a wish as an observation.
        version = `${range} (declared)`;
      }
      packages.push({
        name,
        version,
        kind,
        url: url ?? `https://www.npmjs.com/package/${name}`,
      });
    }
  }
  packages.sort((a, b) => a.name.localeCompare(b.name));
  return {
    node: process.versions.node,
    built_at: new Date().toISOString(),
    packages,
  };
}

export default defineConfig({
  define: {
    __SOURCE_URL__: JSON.stringify(SOURCE_URL),
    __APP_VERSION__: JSON.stringify(process.env.FILEARR_VERSION ?? "dev"),
    __FRONTEND_STACK__: JSON.stringify(readFrontendStack()),
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
