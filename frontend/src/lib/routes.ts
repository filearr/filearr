// UI-T12 — hash-route helpers for the in-page browse view. The browse route is
// `#/browse/<library_id>/<encoded rel path>` — the rel_path is URL-encoded as a
// SINGLE token (its own '/'s become %2F) so it never collides with the route's
// own path separators. Empty path = the library root.
export function browseHash(libraryId: string, path = ""): string {
  return `#/browse/${encodeURIComponent(libraryId)}/${encodeURIComponent(path)}`;
}

/** Parsed browse route, or null when the hash is not a browse route. */
export function parseBrowseHash(hash: string): { libraryId: string; path: string } | null {
  const prefix = "#/browse/";
  if (!hash.startsWith(prefix)) return null;
  const rest = hash.slice(prefix.length);
  const slash = rest.indexOf("/");
  const libRaw = slash === -1 ? rest : rest.slice(0, slash);
  const pathRaw = slash === -1 ? "" : rest.slice(slash + 1);
  let libraryId = "";
  let path = "";
  try {
    libraryId = decodeURIComponent(libRaw);
  } catch {
    libraryId = libRaw;
  }
  try {
    path = decodeURIComponent(pathRaw);
  } catch {
    path = "";
  }
  return { libraryId, path };
}

/** Navigate the app to the browse view for a folder (drives the hash router). */
export function gotoBrowse(libraryId: string, path = ""): void {
  location.hash = browseHash(libraryId, path);
}

// Shell routing (UI-T9) — hash-based so a refresh keeps the current tab and
// back/forward toggles between them. Lives here rather than in App.svelte so
// the mapping is unit-testable; it takes the hash as an argument to stay
// DOM-free.
export type Page =
  | "search"
  | "libraries"
  | "admin"
  | "jobs"
  | "alerts"
  | "browse"
  | "timeline"
  | "reports"
  | "agents"
  | "taxonomy"
  | "filter-builder"
  | "help"
  | "about"
  | "account"
  | "kit";

// Human label per page; `Record<Page, …>` makes a new Page without a label a
// type error. Doubles as the set of plain `#/<page>` routes.
const PAGE_LABELS: Record<Page, string> = {
  search: "Search",
  libraries: "Libraries",
  admin: "Admin",
  jobs: "Jobs",
  alerts: "Alerts",
  browse: "Browse",
  timeline: "Timeline",
  reports: "Reports",
  agents: "Agents",
  taxonomy: "Taxonomy",
  "filter-builder": "Filter builder",
  help: "Help",
  // #/about — the running build stack (versions of everything). Footer-linked
  // rather than a nav tab: it is a reference surface, not a daily workflow.
  about: "About",
  // #/account — self-service profile/password/preferences (header badge link).
  account: "Account",
  // #/kit — UI-kit development gallery. URL only: deliberately absent from the
  // nav and footer, it is an acceptance surface rather than a user feature.
  kit: "UI kit",
};

/** Resolve a location hash to the page (and browse target) it shows. Empty or
 *  unknown hashes fall back to search. `#/browse` alone (no library) is not a
 *  browse route — browse is only reachable with a library id. */
export function routeFromHash(hash: string): { page: Page; browseLib: string; browsePath: string } {
  const browse = parseBrowseHash(hash);
  if (browse) return { page: "browse", browseLib: browse.libraryId, browsePath: browse.path };
  const name = hash.startsWith("#/") ? hash.slice(2) : "";
  const page = name !== "browse" && Object.hasOwn(PAGE_LABELS, name) ? (name as Page) : "search";
  return { page, browseLib: "", browsePath: "" };
}

/** `document.title` for a page, e.g. "Filter builder · Filearr". */
export function pageTitle(page: Page): string {
  return `${PAGE_LABELS[page]} · Filearr`;
}
