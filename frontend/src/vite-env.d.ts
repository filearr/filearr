/// <reference types="svelte" />
/// <reference types="vite/client" />

// Build-time constants injected via Vite `define` (see vite.config.ts).
// __SOURCE_URL__ is the AGPL §13 "Source" link target; __APP_VERSION__ is the
// build version string shown in the footer.
declare const __SOURCE_URL__: string;
declare const __APP_VERSION__: string;
// __FRONTEND_STACK__ is the About page's frontend half: the RESOLVED versions
// of the packages this bundle was built from, plus the Node that built it. The
// image ships only dist/, so build time is the only moment this is knowable —
// hence a baked-in constant rather than an endpoint.
declare const __FRONTEND_STACK__: import("./lib/about").FrontendStack;
