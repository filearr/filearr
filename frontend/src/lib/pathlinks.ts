// UI-T12 — pure link/display-path builders for a library's user-facing network
// location (`share_prefix`) plus a rel_path. NO DOM, NO fetch: trivially
// reviewable and unit-testable. See help.ts `share_prefix` for the three
// supported prefix formats.
//
// HONESTY: the `file://` URLs produced here are commonly BLOCKED by browsers
// (Chrome/Edge silently refuse to navigate to file:// from an http(s) page).
// Every open-location affordance in the UI therefore pairs the link with a
// copy-to-clipboard button of the display path — never assume the link works.

export type PrefixKind = "unc" | "url" | "posix" | "unknown";

/** Schemes a dynamic href may carry: web plus the network-share family. */
export const SAFE_SCHEMES = ["http", "https", "smb", "sftp", "ftp", "ftps", "nfs", "webdav", "webdavs", "file"];

/**
 * Gate for every dynamic `href` (D9/M26): returns `value` only when it is a URL
 * whose scheme is allowed (default SAFE_SCHEMES, case-insensitive) or an
 * absolute path (`/…`) / UNC (`\\…`); otherwise null and the caller renders
 * plain text. `javascript:`, `data:`, `vbscript:` and friends never pass —
 * including with leading whitespace/control chars, which browsers ignore.
 */
export function safeHref(value: unknown, opts: { schemes?: string[] } = {}): string | null {
  if (typeof value !== "string") return null;
  // Strip what the URL parser strips (leading C0/space, tab/CR/LF anywhere),
  // so the check sees what the browser does.
  const v = value.replace(/[\t\n\r]/g, "").replace(/^[\u0000-\u0020]+/, "");
  if (!v) return null;
  const allowed = (opts.schemes ?? SAFE_SCHEMES).map((s) => s.toLowerCase());
  // UNC opens as file:, so it passes only where file is allowed.
  if (v.startsWith("\\\\")) return allowed.includes("file") ? value : null;
  // Absolute path, but not `//host` or `/\host` (protocol-relative to a browser).
  if (v.startsWith("/")) return /^\/[\/\\]/.test(v) ? null : value;
  let url: URL;
  try {
    url = new URL(v);
  } catch {
    return null;
  }
  const scheme = url.protocol.replace(/:$/, "").toLowerCase();
  return allowed.includes(scheme) ? value : null;
}

/** Classify a share_prefix into one of the handled shapes. */
export function classifyPrefix(prefix: string): PrefixKind {
  if (!prefix) return "unknown";
  if (prefix.startsWith("\\\\")) return "unc"; // \\host\share
  // smb://, ftp://, file:// — a scheme outside SAFE_SCHEMES (javascript://x) is unknown.
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(prefix)) return safeHref(prefix) ? "url" : "unknown";
  if (prefix.startsWith("/")) return "posix"; // /Volumes/media
  return "unknown";
}

/** Split a posix rel_path into segments, dropping empties. */
export function relSegments(relPath: string): string[] {
  return (relPath ?? "").split("/").filter(Boolean);
}

/** URL-encode each path segment (spaces, #, %, …) and rejoin with '/'. */
function encodeSegments(relPath: string): string {
  return relSegments(relPath).map(encodeURIComponent).join("/");
}

/**
 * Build an openable URL from a share_prefix + rel_path. Rules:
 *   - UNC `\\host\share` → `file://host/share/<encoded rel>`
 *   - URL scheme `smb://…` (or ftp/nfs/file) → prefix + '/' + `<encoded rel>`
 *   - absolute posix `/Volumes/media` → `file:///Volumes/media/<encoded rel>`
 *   - anything else → "" (no open affordance)
 * Returns "" when a URL cannot be safely built (caller hides the link).
 */
export function buildOpenUrl(prefix: string | null | undefined, relPath = ""): string {
  if (!prefix) return "";
  const kind = classifyPrefix(prefix);
  const rel = encodeSegments(relPath);
  if (kind === "unc") {
    // Drop the leading \\, normalise the rest to '/', encode host + share parts.
    const body = prefix
      .slice(2)
      .replace(/\\/g, "/")
      .split("/")
      .filter(Boolean)
      .map(encodeURIComponent)
      .join("/");
    return `file://${body}${rel ? "/" + rel : ""}`;
  }
  if (kind === "url") {
    const base = prefix.replace(/\/+$/, "");
    return rel ? `${base}/${rel}` : base;
  }
  if (kind === "posix") {
    const body = prefix
      .replace(/\/+$/, "")
      .split("/")
      .map(encodeURIComponent)
      .join("/"); // leading '' preserved → `/Volumes/media`
    return `file://${body}${rel ? "/" + rel : ""}`;
  }
  return "";
}

/**
 * Build a human-facing display path (for showing + copy-to-clipboard) from a
 * share_prefix + rel_path, using NATIVE separators: backslashes for a UNC
 * prefix, forward slashes otherwise. NOT URL-encoded (this is what a person
 * pastes into a file manager). Empty prefix → the rel_path unchanged.
 */
export function buildDisplayPath(prefix: string | null | undefined, relPath = ""): string {
  if (!prefix) return relPath;
  if (classifyPrefix(prefix) === "unc") {
    const base = prefix.replace(/\\+$/, "");
    const rel = relSegments(relPath).join("\\");
    return rel ? `${base}\\${rel}` : base;
  }
  const base = prefix.replace(/\/+$/, "");
  const rel = relSegments(relPath).join("/");
  return rel ? `${base}/${rel}` : base;
}
