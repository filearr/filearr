// Thin client for the Filearr API (search goes through the backend, which
// translates flat params into Meilisearch filter syntax).

// The agent-policy document shape + its field metadata live in ./agentPolicyDoc
// (a DOM-free module, so the unknown-key round-trip rules that guard against
// silently discarding an operator's forward-compat key are unit-testable on
// Node). Imported + re-exported here so callers keep one types import site.
import type { AgentPolicyDoc } from "./agentPolicyDoc";
// Same split, same reason, for the inventory-collector catalogue: the merge /
// unknown-name-preservation rules behind the config-group dialog's checkbox
// list are DOM-free so they can be unit-tested on Node.
import type { CollectorCatalogueEntry } from "./inventoryCollectors";
// The host-tool minimum-version catalogue, next to the chip logic that reads it.
import type { HostToolMinimum } from "./hostTools";
// The About page's response shape lives in ./about alongside the pure render
// helpers that consume it; re-exported here so callers keep one types import.
import type { About } from "./about";
// The PER-AGENT About report, same split for the same reason: the cell rules
// ("not reported" vs "version unknown" vs "not installed") and the Markdown
// dump are pure, so they are unit-tested on Node.
import type { AgentAbout } from "./agentAbout";

export type { About } from "./about";
export type {
  AgentAbout,
  AgentAboutBuild,
  AgentAboutIdentity,
  AgentAboutModule,
  AgentAboutTool,
} from "./agentAbout";
export type { CollectorCatalogueEntry } from "./inventoryCollectors";
export type { HostToolMinimum } from "./hostTools";

// A single search hit is an untyped Meili document plus, for document results,
// P3-T5 ``snippet`` (cropped body text with <em>…</em> match markers) and
// ``highlight`` (title/filename markers). Both are rendered SAFELY on the client
// (text nodes + <mark>, never {@html}); the raw ``body_text`` is stripped
// server-side so a response never ships kilobytes of body per row.
export type SearchHit = Record<string, unknown> & {
  snippet?: string;
  highlight?: { title?: string; filename?: string };
  // File-group facet value (archive / source-code / ebook / raw-photo / …),
  // derived server-side from the extension. Filterable + facetable, mirroring
  // ``media_type``.
  file_group?: string;
};

export interface SearchResponse {
  hits: SearchHit[];
  total: number;
  facets: Record<string, Record<string, number>>;
  // P3-T4: per-numeric-facet min/max from Meili facetStats (size/mtime). Empty
  // when the engine returns no stats (e.g. an empty result set). Drives the
  // range-slider bounds — never hardcoded.
  facet_stats: Record<string, { min: number; max: number }>;
  next_cursor: string | null;
}

const KEY = () => localStorage.getItem("apiKey") ?? "";

/** The stored API key, if any. Empty string when auth is disabled / no key set. */
export const apiKey = (): string => KEY();

/** API base path (shared by fetch requests and the SSE EventSource URL). */
export const API_BASE = "/api/v1";

/** Immutable, content-addressed thumbnail URL for an item + tier (S12/P12).
 *  Used as an ``<img src>``, which cannot send an Authorization header, so the
 *  read-scope key rides as ``?api_key=`` exactly like the SSE events stream.
 *  ``tier`` is one of the serve-endpoint enum values ("grid" | "preview"). */
export function thumbUrl(id: string, tier: "grid" | "preview" = "grid"): string {
  const key = KEY();
  const auth = key ? `&api_key=${encodeURIComponent(key)}` : "";
  return `${API_BASE}/items/${id}/thumb?tier=${tier}${auth}`;
}

/** An HTTP error carrying the numeric status, so callers can branch on 401/403/
 *  404 (P6-T4 RBAC). ``message`` keeps the legacy ``"<status>: <body>"`` shape
 *  so existing ``String(e)`` render paths are unchanged. */
export class ApiError extends Error {
  status: number;
  body: string;
  constructor(status: number, body: string) {
    super(`${status}: ${body}`);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

/** Map an error to a friendly, non-technical sentence for RBAC-denied surfaces
 *  (403 = permission, 404 = not visible/gone) — so a scoped user sees a clear
 *  message instead of a blank pane or a raw status dump. Falls back to the raw
 *  message for anything else. */
export function friendlyError(e: unknown, verb = "view"): string {
  if (e instanceof ApiError) {
    if (e.status === 403) return `You don't have permission to ${verb} this item.`;
    if (e.status === 404) return "This item is not available (it may be outside your access or removed).";
    if (e.status === 401) return "Please sign in to continue.";
  }
  return String(e);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}),
      ...init?.headers,
    },
  });
  if (!res.ok) throw new ApiError(res.status, await res.text());
  return res.json() as Promise<T>;
}

// W8: type filtering is now the two-level file taxonomy — ``file_category``
// (coarse, ~9 categories) and ``file_group`` (granular). Both are REPEATABLE
// backend params; the legacy single-select ``media_type``/``type`` param is gone.
// A flat param record (deep-link hash / saved search) cannot hold repeated keys,
// so each multi-select rides as ONE comma-joined value here and is expanded into
// ``key=a&key=b`` for the backend's List[str].
//
// NOTE (forward-looking): ``file_category`` + ``file_group`` are intentionally
// first-class in this flat param vocabulary so the future visual filter builder
// can emit/consume them 1:1 with the search filters (see FilterBuilderPage).
const REPEATABLE_SEARCH_PARAMS = ["file_category", "file_group"] as const;

export function search(
  params: Record<string, string>,
  signal?: AbortSignal,
): Promise<SearchResponse> {
  const qs = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== ""));
  for (const key of REPEATABLE_SEARCH_PARAMS) {
    const joined = qs.get(key);
    if (joined == null) continue;
    qs.delete(key);
    for (const v of joined.split(",").map((s) => s.trim()).filter(Boolean)) {
      qs.append(key, v);
    }
  }
  return request(`/search?${qs}`, { signal });
}

/** Single-item metadata edit (IN-T4). Absent key = untouched, explicit ``null``
 *  = clear — including key-by-key inside ``user_metadata``. Returns the item
 *  through the same GPS-gated projection as GET, so the caller can render the
 *  result without a second fetch. */
export function patchItem(id: string, patch: ItemPatchBody) {
  return request<ItemRecord>(`/items/${id}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

// ---- IN-T4 bulk metadata edit -----------------------------------------------
/** One item's patch on the wire. ABSENT key = untouched, explicit ``null`` =
 *  clear (both for the scalar columns and, key-by-key, inside user_metadata).
 *  ``tags`` REPLACES the whole list — the add/remove arithmetic is done per item
 *  on the client (see ./bulkEdit.ts) precisely because of that. */
export interface ItemPatchBody {
  title?: string | null;
  year?: number | null;
  tags?: string[];
  user_metadata?: Record<string, unknown>;
  external_ids?: Record<string, unknown>;
}

/** ``POST /items/batch`` response: one result per requested id. ``"ok"`` on
 *  success; a string ``"error: …"`` for per-item RBAC/lookup failures; a
 *  structured object for a custom-field validation rejection. The caller MUST
 *  surface the failures — see summarizeResults() in ./bulkEdit.ts. */
export type BatchItemResult =
  | "ok"
  | string
  | { error: string; detail: unknown[] };

export interface BatchPatchResponse {
  results: Record<string, BatchItemResult>;
}

/** Apply a DIFFERENT patch to each of up to 500 items in one request.
 *  Callers chunk at ``BATCH_CHUNK`` (500) — the server rejects a larger map with
 *  a 413, so chunking is the contract, not an optimisation. Partial failure is
 *  normal and is reported per item, never as a request-level error. */
export const batchPatchItems = (patches: Record<string, ItemPatchBody>) =>
  request<BatchPatchResponse>("/items/batch", {
    method: "POST",
    body: JSON.stringify(patches),
  });

/** A full single-item record: every stored column, with ``metadata`` and
 *  ``user_metadata`` returned as separate unmerged objects. Backs the Raw tab. */
export type ItemRecord = Record<string, unknown> & {
  // P10-T11/T12: the item's resolved network location (e.g. ``smb://…``) and the
  // tier that produced it. Resolution precedence: agent hint > admin mapping >
  // library share_prefix. ``share_url`` is null when no location resolves — the
  // UI then renders NO open affordance (never a fabricated/empty location).
  share_url?: string | null;
  share_source?: "agent_hint" | "mapping" | "library" | null;
};

/** Fetch one item with every stored field. Powers the Raw detail view. */
export const getItem = (id: string) => request<ItemRecord>(`/items/${id}`);

/** Frecency use ping (fire-and-forget; 204 with no body). Callers swallow errors. */
export async function touchItem(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/items/${id}/touch`, {
    method: "POST",
    headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
  });
  if (!res.ok) throw new ApiError(res.status, await res.text());
}

/** P10-T3: ask the owning agent to re-verify an agent-hosted item's existence
 *  (``stat``) or integrity (``rehash``). Returns the created agent_commands row;
 *  the result lands later via a normal item refresh once the agent completes it. */
export const verifyItem = (id: string, mode: "stat" | "rehash") =>
  request<{ id: string; kind: string; status: string; mode: string }>(
    `/items/${id}/verify`,
    { method: "POST", body: JSON.stringify({ mode }) },
  );

// ---- P10-T10 — hosting-agent identity / online status / verify freshness -----
/** ``GET /items/{id}/agent-status``. For a centrally-scanned item only
 *  ``{agent_hosted:false}`` comes back; for an agent-hosted item the panel fields
 *  are present. ``online`` is ``last_seen_at`` within the server's online window. */
export interface ItemAgentStatus {
  agent_hosted: boolean;
  agent_id?: string;
  agent_name?: string;
  agent_status?: "active" | "revoked" | "pending";
  online?: boolean;
  last_seen_at?: string | null;
  last_verified_at?: string | null;
  verify_in_flight?: boolean;
}

export const itemAgentStatus = (id: string) =>
  request<ItemAgentStatus>(`/items/${id}/agent-status`);

// ---- P10-T6/T7/T13 — agent file retrieve (transfer) --------------------------
export type TransferState =
  | "pending"
  | "uploading"
  | "staged"
  | "downloaded"
  | "expired"
  | "failed";

/** ``GET /transfers/{id}`` status payload (also the shape the SSE frames mirror). */
export interface TransferStatus {
  transfer_id: string;
  item_id: string;
  agent_id: string;
  state: TransferState;
  verified: boolean;
  bytes_transferred: number;
  total_bytes: number | null;
  created_at: string | null;
  expires_at: string | null;
  last_range_request_at: string | null;
}

/** One SSE frame: the status payload + the P10-T7 derived ``waiting_for_agent``
 *  pseudo-state, plus ``reason`` (terminal frames) / ``detail`` (error frames). */
export interface TransferEvent extends Partial<TransferStatus> {
  waiting_for_agent?: boolean;
  reason?: string;
  detail?: string;
}

/** Initiate an agent→central retrieve (P10-T13). A 202 returns the new transfer;
 *  a 409 "an active transfer already exists" is NOT an error — its id is parsed
 *  out of the detail so the caller attaches to the in-flight transfer instead
 *  (``existing: true``). Any other failure propagates. */
export async function initiateTransfer(
  itemId: string,
  verifyHash = true,
): Promise<{ transfer_id: string; state: string; existing: boolean }> {
  try {
    const r = await request<{ transfer_id: string; state: string }>(
      `/items/${itemId}/transfer`,
      { method: "POST", body: JSON.stringify({ verify_hash: verifyHash }) },
    );
    return { ...r, existing: false };
  } catch (e) {
    if (e instanceof ApiError && e.status === 409) {
      const m = e.body.match(
        /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i,
      );
      if (m) return { transfer_id: m[0], state: "pending", existing: true };
    }
    throw e;
  }
}

export const getTransfer = (id: string) =>
  request<TransferStatus>(`/transfers/${id}`);

export const cancelTransfer = (id: string) =>
  request<{ transfer_id: string; state: string }>(`/transfers/${id}`, {
    method: "DELETE",
  });

/** SSE URL for a transfer's progress stream. ``EventSource`` can't set headers,
 *  so a single-use scoped stream token (minted per connect via
 *  ``mintTransferEventsToken``) rides as ``?stream_token=`` — never the real
 *  API key (query strings land in proxy logs). Tokenless when auth is off. */
export function transferEventsUrl(id: string, streamToken = ""): string {
  const qs = streamToken ? `?stream_token=${encodeURIComponent(streamToken)}` : "";
  return `${API_BASE}/transfers/${id}/events${qs}`;
}

/** Mint a single-use, 60 s token scoped to one transfer's SSE stream. Callers
 *  mint a fresh one per (re)connect — the token is consumed on first use. */
export const mintTransferEventsToken = (id: string) =>
  request<{ token: string; expires_in: number }>(
    `/transfers/${id}/events-token`,
    { method: "POST" },
  );

/** Fetch the verified staged file (auth header) and save it as ``filename``.
 *  Mirrors ``downloadExport`` — a blob save so the Bearer header is sent (an
 *  ``<a href>`` cannot), served only for a verified, staged/downloaded transfer. */
export async function downloadTransfer(id: string, filename: string): Promise<void> {
  const res = await fetch(`${API_BASE}/transfers/${id}/download`, {
    headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
  });
  await saveBlob(res, filename || `transfer-${id}`);
}

// P3-T9 — related / near-duplicate items via the semantic vector. Returns 409
// (thrown as an error by ``request``) when semantic search is disabled server-side
// or the item is not yet embedded; callers treat that as "unavailable".
export interface SimilarResponse {
  id: string;
  hits: SearchHit[];
}
export const similarItems = (id: string, limit = 10) =>
  request<SimilarResponse>(`/items/${id}/similar?limit=${limit}`);

// P3-T8 — the /stats semantic coverage section (present but disabled=false when
// FILEARR_SEMANTIC_ENABLED is off). Drives the hidden-unless-enabled UI affordances.
export interface SemanticStats {
  enabled: boolean;
  model: string;
  embedded_count: number;
  pending: number;
  fp_mismatches: number;
}
export const semanticStats = async (): Promise<SemanticStats | null> => {
  try {
    const r = (await stats()) as { semantic?: SemanticStats };
    return r.semantic ?? null;
  } catch {
    return null;
  }
};

// ---- admin ----
export type HashPolicy = "auto" | "full" | "quick_only";

// FIX-10: most-recent ScanRun for a library, sourced per-library from scan_runs
// (survives redeploys; not subject to the capped global /scans feed). Null only
// when the library has genuinely never been scanned.
export interface LastScan {
  started_at: string;
  finished_at: string | null;
  status: string;
  seen?: number | null;
  new?: number | null;
  changed?: number | null;
  missing?: number | null;
  /** Files the walk enumerated but did NOT ingest: `seen + excluded` = files
   *  enumerated. Split by cause so a gap between an OS folder count and the
   *  library total is explainable. */
  excluded?: number | null;
  /** Rejected by the library's category/group selection. */
  excluded_gate?: number | null;
  /** Rejected by the exclusion spec (presets / exclude_globs / dotfiles). */
  excluded_filtered?: number | null;
  /** Directories skipped wholesale — their CONTENTS are never enumerated, so a
   *  non-zero value means the file accounting is a lower bound. */
  pruned_dirs?: number | null;
  /** Directories that could not be read at all (same lower-bound caveat). */
  permission_denied?: number | null;
  /** Total on-disk bytes walked. */
  bytes_seen?: number | null;
  /** Files inside pruned subtrees. Only meaningful when `pruned_counted` is
   *  true (the library's `count_pruned_files` opt-in); otherwise pruned trees
   *  are never enumerated, this stays 0, and `seen + excluded` is a LOWER BOUND
   *  on what is actually on disk. */
  pruned_files?: number | null;
  pruned_counted?: boolean | null;
  /** Capped sample of pruned directory paths, so the UI can name the culprits
   *  (".git", ".venv") instead of showing an opaque count. */
  pruned_paths?: string[] | null;
  /** §17 throughput: this run's walk rate, the library's rolling median over
   *  recent finished FULL scans (30-day window), and how many runs back that
   *  median. Drives the "slower than usual" badge (only when runs >= 3). */
  files_per_s?: number | null;
  median_files_per_s?: number | null;
  throughput_runs?: number;
}

/** Agent-owned library annotation (GET /libraries): the owning agent's console
 *  identity + freshness. Central never scans these libraries, so the honest
 *  "last activity" is the agent's replication heartbeat + reconcile watermark.
 *  All null on centrally-scanned libraries. Declared on Library below. */

export interface Library {
  id: string;
  name: string;
  root_path: string;
  native_prefix: string | null;
  share_prefix: string | null;
  // W8: two-level file taxonomy gating (replaces the old ``enabled_types``).
  // ``enabled_categories`` = category keys (each includes ALL its groups);
  // ``enabled_groups`` = individually-included group keys. Empty both = all types.
  enabled_categories: string[];
  enabled_groups: string[];
  include_globs: string[];
  exclude_globs: string[];
  enabled_presets: string[];
  enabled_extension_groups: string[];
  scan_cron: string | null;
  watch_mode: boolean;
  hash_policy: HashPolicy;
  hash_full_max_bytes: number | null;
  ocr_enabled: boolean;
  chunking_enabled: boolean;
  expose_gps: boolean;
  /** Opt-in: also enumerate PRUNED subtrees (cheap count, no ingest) so
   *  seen + excluded + pruned_files reconciles with the on-disk file count.
   *  Off by default — the extra directory listing is slow on rclone/SMB. */
  count_pruned_files: boolean;
  enabled: boolean;
  last_scan: LastScan | null;
  // OPS-T7: effective user-facing share prefix + provenance. ``share_prefix``
  // above is the raw manual override; these are computed server-side (manual
  // wins, else the deploy mount map covering the library root, else none).
  share_prefix_effective: string | null;
  share_prefix_source: "manual" | "mount-map" | "none";
  // UI-T15: Windows-UNC counterpart of ``share_prefix_effective`` (null when the
  // location has no UNC form). The UI renders whichever spelling the viewer's OS
  // wants; see lib/osFormat.ts.
  share_unc_effective: string | null;
  /** P5-T4: non-null when this library's content is owned by a remote agent
   *  (replicated in; central never scans it — scan controls are refused). */
  source_agent_id: string | null;
  /** Agent-owned annotation: owning agent's name/status + replication
   *  heartbeat and reconcile watermark (the honest "last sync" — central
   *  never scans these). All null for centrally-scanned libraries. */
  agent_name: string | null;
  agent_status: string | null;
  agent_last_seen_at: string | null;
  agent_last_reconcile_at: string | null;
}

export interface ScanRun {
  id: string;
  library_id: string;
  started_at: string;
  finished_at: string | null;
  status: string;
  stats: Record<string, number>;
}

// ---- P2-T5 presets / extension groups (read-only catalogue) ----
export interface Preset {
  name: string;
  label: string;
  patterns: string[];
  default_enabled: boolean;
  caveat: string | null;
}

export interface ExtensionGroup {
  name: string;
  label: string;
  file_category: string;
  extensions: string[];
}

export interface PresetsResponse {
  presets: Preset[];
  extension_groups: ExtensionGroup[];
}

/** The code-constant preset bundles + extension groups (read scope). */
export const listPresets = () => request<PresetsResponse>("/presets");

// ---- file groups (controlled vocabulary for the search file_group facet) ----
// One filterable category derived from the file extension (e.g. archive,
// source-code, ebook, raw-photo). ``media_type`` is the broad type the group
// rolls up under; ``extensions`` is the membership list (informational — the UI
// filters by ``id``, never by re-deriving from extensions client-side).
export interface FileGroup {
  id: string;
  label: string;
  file_category: string;
  description: string;
  extensions: string[];
}

/** The file-group vocabulary that populates the search file_group facet (read
 *  scope). Mirrors the extension-group catalogue; callers should fall back to a
 *  small static list if this fails so the filter still renders. */
export const fileGroups = () => request<FileGroup[]>("/system/file-groups");

// --------------------------------------------------------------------------- //
// W8 — file-extension similarity taxonomy (category -> group -> extensions).   //
// The taxonomy is the source of truth for type gating (libraries) and type     //
// filtering (search). Mutations are admin-scoped and return the bumped         //
// ``version`` so a UI can show/track the current schema revision.              //
// --------------------------------------------------------------------------- //

/** Extractor a category routes its files to (or none). Mirrors the media_types
 *  extractor families; drives which per-type extractor a scan runs. */
export const TAXONOMY_EXTRACTORS = [
  "image", "audio", "video", "document", "model3d", "none",
] as const;
export type TaxonomyExtractor = (typeof TAXONOMY_EXTRACTORS)[number];

export interface TaxonomyCategory {
  key: string;
  label: string;
  description: string;
  /** One of TAXONOMY_EXTRACTORS (kept as string — an older/newer backend may
   *  add families the UI hasn't enumerated). */
  extractor: string;
  sort_order: number;
  is_builtin: boolean;
}

export interface TaxonomyGroup {
  key: string;
  label: string;
  description: string;
  sort_order: number;
  is_builtin: boolean;
  extensions: string[];
}

/** One category with its ordered groups (the tree node shape). */
export interface TaxonomyNode {
  category: TaxonomyCategory;
  groups: TaxonomyGroup[];
}

export interface TaxonomyTree {
  version: number;
  tree: TaxonomyNode[];
}

/** Every mutation echoes the new schema ``version``. */
export interface TaxonomyVersion {
  version: number;
}

/** Adding an extension is an UPSERT: if it already belonged to another group the
 *  server reparents it and reports the ``previous_group`` it moved from. */
export interface ExtensionUpsertResult extends TaxonomyVersion {
  ext: string;
  group_key: string;
  previous_group: string | null;
}

/** The full taxonomy tree + current version (read scope). Callers degrade to an
 *  empty tree so the type filters / library gating still render if it fails. */
export const getTaxonomy = () => request<TaxonomyTree>("/taxonomy");

export const createTaxonomyCategory = (body: {
  key: string;
  label: string;
  description?: string;
  extractor?: string;
  sort_order?: number;
}) => request<TaxonomyVersion>("/taxonomy/categories", { method: "POST", body: JSON.stringify(body) });

export const updateTaxonomyCategory = (
  key: string,
  patch: Partial<{ label: string; description: string; extractor: string; sort_order: number }>,
) => request<TaxonomyVersion>(`/taxonomy/categories/${encodeURIComponent(key)}`, {
  method: "PATCH",
  body: JSON.stringify(patch),
});

/** DELETE a category. 409 if it still has groups (reparent/remove them first). */
export const deleteTaxonomyCategory = (key: string) =>
  request<TaxonomyVersion>(`/taxonomy/categories/${encodeURIComponent(key)}`, { method: "DELETE" });

export const createTaxonomyGroup = (body: {
  key: string;
  label: string;
  description?: string;
  category_key: string;
  sort_order?: number;
}) => request<TaxonomyVersion>("/taxonomy/groups", { method: "POST", body: JSON.stringify(body) });

/** PATCH a group — including ``category_key`` to REPARENT it under another category. */
export const updateTaxonomyGroup = (
  key: string,
  patch: Partial<{ label: string; description: string; category_key: string; sort_order: number }>,
) => request<TaxonomyVersion>(`/taxonomy/groups/${encodeURIComponent(key)}`, {
  method: "PATCH",
  body: JSON.stringify(patch),
});

export const deleteTaxonomyGroup = (key: string) =>
  request<TaxonomyVersion>(`/taxonomy/groups/${encodeURIComponent(key)}`, { method: "DELETE" });

/** Add (or MOVE) an extension onto a group. Upsert: reparents if it already
 *  existed, returning ``previous_group``. 422 on a bad ext (^[a-z0-9_+-]{1,32}$). */
export const addTaxonomyExtension = (groupKey: string, ext: string) =>
  request<ExtensionUpsertResult>(
    `/taxonomy/groups/${encodeURIComponent(groupKey)}/extensions`,
    { method: "POST", body: JSON.stringify({ ext }) },
  );

export const deleteTaxonomyExtension = (ext: string) =>
  request<TaxonomyVersion>(`/taxonomy/extensions/${encodeURIComponent(ext)}`, { method: "DELETE" });

export const listLibraries = () => request<Library[]>("/libraries");

// OPS-T7: the deploy-time network-share mount map (read scope). Credential-free;
// ``share_url`` is a user-facing reference the library form surfaces as a hint.
export interface ShareMapEntry {
  container_prefix: string;
  share_url: string;
  storage_type: string | null;
  host: string | null;
  unc?: string | null;
}

export const listShareMap = () => request<ShareMapEntry[]>("/system/share-map");

/** Longest-container_prefix-wins client mirror of share_map.resolve — used to
 *  preview the auto share_prefix a library root would inherit from the deploy. */
export function resolveShareHint(
  map: ShareMapEntry[],
  rootPath: string,
): ShareMapEntry | null {
  const norm = (p: string) => p.replace(/\\/g, "/").replace(/^\/+|\/+$/g, "");
  const target = norm(rootPath);
  let best: ShareMapEntry | null = null;
  let bestLen = -1;
  for (const e of map) {
    const base = norm(e.container_prefix);
    const covers = base === "" || target === base || target.startsWith(base + "/");
    if (covers && base.length > bestLen) {
      best = e;
      bestLen = base.length;
    }
  }
  if (!best) return null;
  // Append the mount-relative remainder so a library rooted at a SUBFOLDER of
  // the mount shows its true network location (mirror of backend
  // share_map.resolve): mount /data/media -> smb://server/share with root
  // /data/media/information must hint smb://server/share/information.
  const baseSegs = norm(best.container_prefix).split("/").filter(Boolean);
  const remainder = target.split("/").filter(Boolean).slice(baseSegs.length);
  if (remainder.length === 0) return best;
  const joinUrl = (prefix: string, sep: string) =>
    prefix.replace(new RegExp(`[${sep === "\\" ? "\\\\" : sep}/]+$`), "") +
    sep +
    remainder.join(sep);
  return {
    ...best,
    share_url: joinUrl(best.share_url, "/"),
    unc: best.unc ? joinUrl(best.unc, "\\") : best.unc,
  };
}

export const createLibrary = (body: {
  name: string;
  root_path: string;
  native_prefix?: string | null;
  share_prefix?: string | null;
  enabled_categories?: string[];
  enabled_groups?: string[];
  include_globs?: string[];
  exclude_globs?: string[];
  enabled_presets?: string[];
  enabled_extension_groups?: string[];
  scan_cron?: string | null;
  watch_mode?: boolean;
  hash_policy?: HashPolicy;
  hash_full_max_bytes?: number | null;
  ocr_enabled?: boolean;
  chunking_enabled?: boolean;
  expose_gps?: boolean;
  count_pruned_files?: boolean;
}) => request<Library>("/libraries", { method: "POST", body: JSON.stringify(body) });

// Partial update (scan_cron / watch_mode edits are re-validated server-side; a
// 422 body carries the reason, surfaced by AdminPage's error banner).
export const updateLibrary = (
  id: string,
  patch: Partial<{
    name: string;
    root_path: string;
    native_prefix: string | null;
    share_prefix: string | null;
    enabled_categories: string[];
    enabled_groups: string[];
    include_globs: string[];
    exclude_globs: string[];
    enabled_presets: string[];
    enabled_extension_groups: string[];
    scan_cron: string | null;
    watch_mode: boolean;
    hash_policy: HashPolicy;
    hash_full_max_bytes: number | null;
    ocr_enabled: boolean;
  chunking_enabled: boolean;
    expose_gps: boolean;
    count_pruned_files: boolean;
    enabled: boolean;
  }>,
) => request<Library>(`/libraries/${id}`, { method: "PATCH", body: JSON.stringify(patch) });

/**
 * UI-T2 — hard-delete a library (admin scope). The contract requires an exact
 * name match in `?confirm=`; the server returns 204 on success, 409 while a scan
 * is running, 422 on a confirm mismatch, 404 if unknown. `request()` can't be
 * reused because a 204 carries no JSON body, so we fetch directly and translate
 * the status codes into a thrown Error the dialog can classify.
 */
export async function deleteLibrary(id: string, confirm: string): Promise<void> {
  const res = await fetch(
    `${API_BASE}/libraries/${id}?confirm=${encodeURIComponent(confirm)}`,
    {
      method: "DELETE",
      headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
    },
  );
  if (res.status === 204) return;
  throw new Error(`${res.status}: ${await res.text()}`);
}

// ---- P4-T3 custom fields (admin-defined user_metadata field definitions) ----
export type CustomFieldType =
  | "string" | "integer" | "float" | "boolean" | "date" | "url" | "select";

export const CUSTOM_FIELD_TYPES: CustomFieldType[] = [
  "string", "integer", "float", "boolean", "date", "url", "select",
];

export interface CustomField {
  id: string;
  name: string;
  label: string;
  data_type: CustomFieldType;
  select_options: string[] | null;
  applies_to: string[];
  library_ids: string[];
  facetable: boolean;
  sortable: boolean;
  required: boolean;
  created_at: string;
}

export const listCustomFields = () => request<CustomField[]>("/custom-fields");

export const createCustomField = (body: {
  name: string;
  label: string;
  data_type: CustomFieldType;
  select_options?: string[] | null;
  applies_to?: string[];
  library_ids?: string[];
  facetable?: boolean;
  sortable?: boolean;
  required?: boolean;
}) => request<CustomField>("/custom-fields", { method: "POST", body: JSON.stringify(body) });

// PATCH: name/data_type are IMMUTABLE server-side (a 422 rejects them); only the
// mutable fields below should ever be sent.
export const updateCustomField = (
  id: string,
  patch: Partial<{
    label: string;
    select_options: string[] | null;
    applies_to: string[];
    library_ids: string[];
    facetable: boolean;
    sortable: boolean;
    required: boolean;
  }>,
) => request<CustomField>(`/custom-fields/${id}`, { method: "PATCH", body: JSON.stringify(patch) });

// Soft-delete: drops the definition; existing user_metadata values are untouched.
export async function deleteCustomField(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/custom-fields/${id}`, {
    method: "DELETE",
    headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
  });
  if (res.status === 204) return;
  throw new Error(`${res.status}: ${await res.text()}`);
}

export const scanLibrary = (id: string) =>
  request<{ job_id: number }>(`/libraries/${id}/scan`, { method: "POST" });

/** W9 targeted rescan of a single FILE or a DIRECTORY under a library root. The
 *  `path` is a rel_path ('' = whole library, file or dir); `recursive` descends
 *  into subdirectories (ignored server-side when the path is a file). The target
 *  need NOT be in the catalog yet, but must exist on disk -> a 404 (surfaced by
 *  the caller inline) means the path is absent under the library root. A 202
 *  returns the deferred job id (`scan_id`, null when it coalesced onto an
 *  in-flight scan of the same scope) plus the echoed scope. */
export interface TargetedScanResult {
  scan_id: number | null;
  library_id: string;
  path: string;
  recursive: boolean;
  is_file: boolean;
  coalesced: boolean;
}

export const targetedScan = (
  id: string,
  body: { path: string; recursive?: boolean },
) =>
  request<TargetedScanResult>(`/libraries/${id}/scan/targeted`, {
    method: "POST",
    body: JSON.stringify({ path: body.path, recursive: body.recursive ?? true }),
  });

export const listScans = () => request<ScanRun[]>("/scans");

export const stats = () => request<Record<string, unknown>>("/stats");

export const cancelScan = (id: string) =>
  request<{ status: string }>(`/scans/${id}/cancel`, { method: "POST" });

// UI-T13 graceful stop: finish the current batch + wrap-up (no tombstoning),
// keep everything scanned so far, end the run "stopped". Distinct from cancel
// (hard abort). Idempotent server-side; 409 if the scan is not running.
export const stopScan = (id: string) =>
  request<{ status: string }>(`/scans/${id}/stop`, { method: "POST" });

// FIX-15 force-clear: an admin escape hatch for a ScanRun wedged non-terminal
// ('stopping' that was never observed, or 'running' orphaned by a dead worker).
// Drives it terminal ('stopped'); refuses (409) only a genuinely-active run
// (live worker present -> use stopScan). Admin-scoped + audited server-side.
export const forceClearScan = (id: string) =>
  request<{ status: string; previous_status: string }>(
    `/scans/${id}/force-clear`,
    { method: "POST" },
  );

/**
 * URL for the scan-progress SSE stream. `EventSource` cannot set an
 * Authorization header, so a single-use scoped stream token (minted per
 * connect via `mintScanEventsToken`) rides as `?stream_token=` — never the
 * real API key (query strings land in proxy logs). When auth is disabled
 * (dev) the stream needs no token at all.
 */
export function scanEventsUrl(id: string, streamToken = ""): string {
  const qs = streamToken ? `?stream_token=${encodeURIComponent(streamToken)}` : "";
  return `${API_BASE}/scans/${id}/events${qs}`;
}

/** Mint a single-use, 60 s token scoped to one scan's SSE stream. Callers
 *  mint a fresh one per (re)connect — the token is consumed on first use. */
export const mintScanEventsToken = (id: string) =>
  request<{ token: string; expires_in: number }>(`/scans/${id}/events-token`, {
    method: "POST",
  });

// ---- UI-T4 server-side folder browser ----
export interface FsEntry {
  name: string;
  path: string;
}

/** `GET /fs/browse` payload: an allowlisted, symlink-safe directory listing.
 *  Empty `path` lists the configured roots. `parent` is null at a root. */
export interface FsBrowse {
  path: string;
  parent: string | null;
  roots: string[];
  dirs: FsEntry[];
}

/** Browse directories under the server's allowlist. A path outside the allowlist
 *  (or a traversal attempt) yields a 422 the picker surfaces inline. */
export const browseFs = (path = "") =>
  request<FsBrowse>(`/fs/browse?path=${encodeURIComponent(path)}`);

// ---- T11 error surfacing ----
export interface FailingItem {
  id: string;
  rel_path: string;
  error: string;
  /** dependency (deployment bug) | guard (intentional ceiling) | corrupt (bad
   *  file) | error (I/O/unexpected; also pre-classification rows). */
  kind: "dependency" | "guard" | "corrupt" | "error";
}

export interface LibraryErrors {
  library_id: string;
  count: number;
  items: FailingItem[];
}

export interface FailedJob {
  id: string;
  queue: string;
  task: string;
  status: string;
  attempts: number | null;
  /** FIX-12: task's genuine-failure retry budget (null = no retry). */
  retry_cap: number | null;
  scheduled_at: string | null;
  attempted_at: string | null;
  /** Sanitized failure message recorded by the §18 worker middleware; null for
   *  jobs that failed before it existed. */
  error: string | null;
  /** Length-capped traceback matching `error` (same provenance/nullability). */
  traceback: string | null;
}

/** Paginated failed-jobs response (FIX-8). ``total`` is the full failed-row
 *  count so the UI can render a real pager; ``items`` is the requested page. */
export interface FailedJobPage {
  items: FailedJob[];
  total: number;
  limit: number;
  offset: number;
}

/** Per-library extraction-error count + a paginated page of failing items. */
export const libraryErrors = (id: string, limit = 50, offset = 0) =>
  request<LibraryErrors>(`/libraries/${id}/errors?limit=${limit}&offset=${offset}`);

/** Library diagnosis report (2026-08-16): ordered verdicts + raw sections.
 *  Write scope. See docs-site/troubleshooting/library-failures.md. */
export interface LibraryVerdict {
  severity: "error" | "warn" | "info" | "ok";
  code: string;
  title: string;
  detail: string;
  actions: string[];
  evidence: Record<string, unknown>;
  doc: string;
}
export interface LibraryDiagnosis {
  generated_at: string;
  docs_url: string;
  library: {
    id: string; name: string; root_path: string; enabled: boolean; watch_mode: boolean;
    scan_cron: string | null; hash_policy: string; source_agent_id: string | null;
    native_prefix: string | null; share_prefix: string | null;
  };
  verdicts: LibraryVerdict[];
  path: Record<string, unknown> & {
    root_path: string; exists?: boolean | null; is_dir?: boolean | null; readable?: boolean | null;
    listing_ms?: number | null; entries_seen?: number; sample?: { name: string; dir: boolean }[];
    error?: string | null; network?: boolean | null; fstype?: string | null; empty?: boolean | null;
    timeout?: boolean; skipped?: string;
  };
  scans: {
    id: string; status: string; started_at: string | null; finished_at: string | null;
    duration_s: number | null; rel_path: string | null; error: string | null;
    stats: Record<string, unknown>;
  }[];
  extract_errors: { count: number; by_kind: Record<string, number>; top_messages: { message: string; count: number }[] };
  failed_jobs: { id: string; task: string; queue: string; attempts: number | null; scheduled_at: string | null; error: string | null }[];
  agent: Record<string, unknown> | null;
  logs: { ts: string | null; source: string; level: string; logger: string; message: string }[];
  context: Record<string, unknown>;
}
export const diagnoseLibrary = (id: string) =>
  request<LibraryDiagnosis>(`/libraries/${id}/diagnose`);

/** Clear stored extraction errors for a library and re-defer extraction for the
 *  affected items (plus any never-hashed items). Returns the number requeued. */
export const retryExtracts = (id: string) =>
  request<{ library_id: string; retried: number }>(
    `/libraries/${id}/retry-extracts`,
    { method: "POST" },
  );

/** Paginated failed Procrastinate jobs (read scope; page capped server-side at
 *  100). Returns {items, total, limit, offset} so the caller can render a pager
 *  (FIX-8 — the list used to grow unbounded on screen). */
export const failedJobs = (limit = 25, offset = 0) =>
  request<FailedJobPage>(`/system/failed-jobs?limit=${limit}&offset=${offset}`);

/** Delete failed Procrastinate rows now (FIX-8, admin scope). Optional ``queue``
 *  scopes the wipe to one queue. Returns the number deleted. */
export const clearFailedJobs = (queue?: string) =>
  request<{ deleted: number; queue: string | null }>(
    "/system/jobs/clear-failed",
    { method: "POST", body: JSON.stringify(queue ? { queue } : {}) },
  );

/** Running-build identity: package version + deploy build stamp (null in dev) +
 *  AGPL §13 source_url (FILEARR_SOURCE_URL). */
export const getVersion = () =>
  request<{
    app_version: string;
    build_stamp: string | null;
    source_url?: string;
    agents_enabled?: boolean; // P5-T1: gates the Admin -> Agents panel
  }>("/version");

// ---- UI-T10 jobs dashboard ----
export interface RunningJob {
  id: string;
  queue: string;
  task: string;
  args: Record<string, unknown>;
  started_at: string | null;
  seconds_running: number | null;
  attempts: number;
  /** FIX-12: task's genuine-failure retry budget (null = no retry). */
  retry_cap: number | null;
  worker_id: number | null;
  worker_alive: boolean;
  stalled: boolean;
  rel_path: string | null;
  /** File size of the job's item (bytes), when it carries a resolvable item_id.
   *  Null otherwise. The UI appends it to thumbnail rows (size predicts duration). */
  size: number | null;
  library_name: string | null;
}

export interface ScanRunning {
  id: string;
  library_id: string;
  library_name: string;
  rel_path: string | null;
  /** ISO start time — the dashboard derives files/min as `stats.seen` over the
   *  elapsed wall time, matching the SSE endpoint's own `rate`. */
  started_at: string | null;
  stats: Record<string, number>;
}

export interface MeiliSnapshot {
  healthy: boolean;
  document_count: number | null;
  is_indexing: boolean | null;
  postgres_active: number;
  drift: number | null;
  in_sync: boolean | null;
  /** Meili-side failed tasks for our index (document writes are fire-and-forget,
   *  so a failed task never fails a queue job — this is the only place it shows). */
  failed_tasks?: number | null;
  last_failed_task?: {
    uid: number | null;
    type: string | null;
    finished_at: string | null;
    code: string | null;
    message: string;
  } | null;
}

export interface ExtractSummary {
  depth: number;
  running: number;
  done: number;
  failed: number;
}

export interface StalledSummary {
  total: number;
  by_queue: Record<string, number>;
}

/** Coarse CPU indicator riding the Jobs poll (NOT a metrics system). All
 *  fields are null on a host without /proc + `os.getloadavg` (Windows /
 *  restricted). */
export interface CpuLoad {
  /** Run-queue load averages — saturation, CAN exceed the core count. */
  load1: number | null;
  load5: number | null;
  load15: number | null;
  cores: number | null;
  /** True all-core utilization %, 0–100: busy/total jiffies delta between
   *  polls (/proc/stat). Null on the first poll (no delta yet). */
  percent: number | null;
}

/** Cumulative disk I/O byte counters (from /proc/diskstats). Rates are computed
 *  client-side between polls. Null off Linux / when /proc is unreadable. */
export interface IoCounters {
  read_bytes: number;
  write_bytes: number;
}

/** Cumulative network byte counters (from /proc/net/dev, all interfaces but lo).
 *  Rates are computed client-side between polls. Null off Linux. */
export interface NetCounters {
  rx_bytes: number;
  tx_bytes: number;
}

/** Cheap Postgres health snapshot (a few catalog reads). Null on any failure
 *  (permissions / odd PG / bare DB) so the tile simply hides. */
export interface DbHealth {
  backends: number;
  active: number;
  idle_in_tx: number;
  waiting: number;
  longest_query_s: number;
  longest_idle_in_tx_s: number;
  /** blks_hit / (hit + read); null when the denominator is 0. */
  cache_hit_ratio: number | null;
  deadlocks: number;
  temp_files: number;
  temp_bytes: number;
  xact_commit: number;
  xact_rollback: number;
  /** Total procrastinate `todo` backlog across queues. */
  queue_backlog: number;
  /** Whole-database on-disk size (pg_database_size). */
  db_size_bytes?: number;
  /** Top tables by total relation size (heap + indexes + toast). */
  largest_tables?: { name: string; bytes: number }[];
}

/** Resource-load section of the Jobs summary. `io`/`net`/`db` are null when
 *  unavailable (non-Linux host, or a failed DB probe) — the tiles self-hide. */
export interface ResourcesSummary {
  cpu: CpuLoad;
  io: IoCounters | null;
  net: NetCounters | null;
  db: DbHealth | null;
}

/** One upcoming scheduled job (per-queue `upcoming` lists, soonest first). */
export interface UpcomingJob {
  label: string;
  /** ISO8601 next-fire instant. */
  at: string;
  task: string;
}

/** Thumbnail-creation monitor: whole-cache totals + the (configurable) thumbs
 *  queue snapshot re-exposed under a stable key. */
export interface ThumbsSummary {
  generated: number;
  bytes: number;
  /** Advisory storage budget in bytes (FILEARR_THUMBNAIL_BUDGET_GB; 0 = none). */
  budget_bytes?: number;
  /** True while `bytes` exceeds the advisory budget — informational only. */
  over_budget?: boolean;
  failed_jobs: number;
  queue: Record<string, number>;
}

/** Aggregate walk throughput across recent COMPLETED scans. Weighted
 *  (SUM(files)/SUM(seconds)), never an average of per-run rates — a tiny scoped
 *  rescan must not outweigh a long full scan. */
export interface ScanThroughput {
  runs: number;
  files: number;
  bytes: number;
  seconds: number;
  files_per_min: number;
  bytes_per_s: number;
  window_days: number;
}

/** One agent actively replicating (seen within the last 10 minutes). By
 *  design replication is NOT a queue job — each agent streams its outbox to
 *  the apply API in its own independent seq-ordered lane — so the Jobs page
 *  surfaces this dedicated activity block instead. */
export interface AgentReplicationActivity {
  id: string;
  name: string;
  last_seen_at: string;
  seq_no: number;
  last_reconcile_at: string | null;
}

export interface JobsSummary {
  /** Agents replicating within the last 10 minutes (empty when feature off). */
  agent_replication: AgentReplicationActivity[];
  queues: Record<string, Record<string, number>>;
  extract: ExtractSummary;
  running: RunningJob[];
  failed_recent: FailedJob[];
  meili: MeiliSnapshot;
  scans_running: ScanRunning[];
  stalled: StalledSummary;
  /** UI-T14 — per-task-class default job priorities (higher = runs sooner). */
  priorities: Record<string, number>;
  /** UI-T14 — whether the staged scan→extract pipeline is enabled. */
  staged_pipeline: boolean;
  /** FIX-11 — disk-headroom rollup for the low-space banner (piggybacks the
   *  existing Jobs poll). `low` lists only the non-ok watch paths; `paths` is the
   *  full per-path detail for the always-on space indicator. */
  disk: DiskSummary;
  /** Coarse CPU/resource-load indicator (rides the same poll). */
  resources: ResourcesSummary;
  /** Thumbnail-creation monitor (rides the same poll). */
  thumbs: ThumbsSummary;
  /** Aggregate scan walk throughput over recent finished runs. */
  scan_throughput: ScanThroughput;
  /** Per-queue upcoming scheduled work (≤3 soonest each). Absent/empty queues
   *  render no "Upcoming" block. */
  upcoming: Record<string, UpcomingJob[]>;
  /** Global maintenance mode (2026-08-09): the operator switch that suspends
   *  scan/maintenance/report scheduling and tells agents to back off. */
  maintenance: MaintenanceMode;
}

/** Global maintenance-mode state (GET/POST /system/maintenance-mode). */
export interface MaintenanceMode {
  active: boolean;
  reason: string | null;
  started_at: string | null;
}

export const maintenanceMode = () =>
  request<MaintenanceMode>("/system/maintenance-mode");

/** Flip global maintenance mode (admin). While active: schedulers idle, manual
 *  scans 409, agents pause their replication push (local scanning continues). */
export const setMaintenanceMode = (active: boolean, reason?: string) =>
  request<MaintenanceMode>("/system/maintenance-mode", {
    method: "POST",
    body: JSON.stringify({ active, reason: reason || null }),
  });

/** FIX-11 — one monitored filesystem's headroom + policy verdict. */
export interface DiskPathStatus {
  label: string;
  path: string;
  free: number;
  total: number;
  pct_free: number;
  status: "ok" | "warn" | "critical";
  reason: string;
}

/** FIX-11 — Jobs-banner disk rollup (worst status + the non-ok paths only). The
 *  monitor additions also carry `paths`: the full per-path detail (every watch
 *  target, with `used`/`is_pg`) the always-on space indicator renders. `paths`
 *  is optional so an older backend (banner-only payload) never breaks the UI. */
export interface DiskSummary {
  status: "ok" | "warn" | "critical";
  low: DiskPathStatus[];
  paths?: (DiskPathStatus & {
    used: number;
    is_pg: boolean;
    /** Device-dedupe: the watch roles sharing this physical device (tooltip). */
    members?: { label: string; path: string }[];
  })[];
  /** Whether low-space events would actually be DELIVERED: the "System: low
   *  disk space" rule's enabled state + attached channel count. Null when the
   *  lookup failed. */
  alerting?: { enabled: boolean; channels: number } | null;
}

/** FIX-11 — full /system/disk payload (every path, not just the low ones). */
export interface DiskReport {
  status: "ok" | "warn" | "critical";
  paths: (DiskPathStatus & { used: number; is_pg: boolean })[];
}

export const systemDisk = () => request<DiskReport>("/system/disk");

/** Result of a runtime queue-priority bump (UI-T14, admin scope). */
export interface JobPriorityResult {
  queue: string;
  priority: number;
  updated: number;
}

/** Counts returned by the stalled-job reaper (FIX-6). */
export interface ReapResult {
  reaped: number;
  retried: number;
  failed: number;
  pruned_workers: number;
}

/** One composite snapshot for the Jobs tab (read scope). The dashboard polls
 *  THIS single URL every few seconds while the tab is visible. */
export const jobsSummary = () => request<JobsSummary>("/system/jobs/summary");

/** In-flight jobs only (read scope). Rarely needed directly — `jobsSummary`
 *  embeds the same list under `running`. */
export const runningJobs = () => request<RunningJob[]>("/system/jobs/running");

/** Requeue or fail jobs orphaned in `doing` by a dead/restarted worker
 *  (FIX-6, admin scope). Returns the reap counts. */
export const reapStalledJobs = () =>
  request<ReapResult>("/system/jobs/reap", { method: "POST" });

/** One maintenance-registry task with its effective schedule + last-run
 *  status (Jobs page maintenance panel). `cron` is the EFFECTIVE schedule
 *  (override else default); `overridden` marks a custom cron; `editable`
 *  gates the schedule editor; `runnable` gates the Run-now action (minutely
 *  infrastructure ticks are shown but not triggerable). */
export type MaintenanceTask = {
  key: string;
  title: string;
  description: string;
  category: "cleanup" | "integrity" | "monitors" | "system" | "ondemand";
  queue: string;
  cron: string | null;
  default_cron: string | null;
  overridden: boolean;
  enabled: boolean;
  editable: boolean;
  runnable: boolean;
  next_run_at: string | null;
  last_run: {
    job_id: number;
    status: string;
    at: string | null;
    /** When the latest attempt began; basis for live elapsed while `doing`. */
    started_at?: string | null;
    /** Wall time of the last completed attempt (started → succeeded/failed). */
    duration_seconds?: number | null;
  } | null;
  /** Present ONLY when the task's optional feature is currently OFF, i.e. a
   *  run would immediately no-op (semantic search disabled, no library has
   *  RAG chunking, …). Absent when the task would really do work — the queue
   *  stores no task result, so this pre-flight gate state is the only honest
   *  explanation the UI can give for a 0-second run. */
  gate?: { enabled: boolean; reason: string } | null;
};

/** Every registered maintenance task (read scope), registry order. */
export const listMaintenance = () =>
  request<{ tasks: MaintenanceTask[] }>("/system/maintenance");

/** Trigger one maintenance task now (admin). 409 = already queued / not
 *  triggerable; the caller should surface `ApiError.body` as the message. */
export const runMaintenance = (key: string) =>
  request<{ job_id: number | null }>(`/system/maintenance/${key}/run`, {
    method: "POST",
  });

// ---- BK-T3 in-app backup ----
// A bundle produced INSIDE the container. `complete` is always false for these
// and the API repeats why in `incomplete_note`: a container cannot read the
// host .env (FILEARR_SECRET_KEY) or the step-ca volume, and a restore under a
// different secret key succeeds while silently orphaning every encrypted
// alert-channel secret. The UI must never present one of these as a full
// disaster-recovery backup.
export interface BackupBundle {
  name: string;
  bytes: number;
  dump_file: string | null;
  created_at: string | null;
  item_count: number | null;
  alembic_head: string | null;
  app_version: string | null;
  complete: boolean;
  missing: string[];
}

/** Queue an in-app backup (admin). 409 when one is already queued/running. */
export const runBackup = () =>
  request<{ job_id: number | null; incomplete_note: string }>("/system/backup", {
    method: "POST",
  });

/** List in-app backup bundles, newest first (admin). */
export const listBackups = () =>
  request<{
    bundles: BackupBundle[];
    dir: string;
    keep: number;
    incomplete_note: string;
  }>("/system/backups");

/** Download one bundle's Postgres dump. Blob save, not an `<a href>`: the
 *  endpoint is admin-scoped and a plain link cannot carry the Bearer header
 *  (same reasoning as `downloadExport`). */
export async function downloadBackup(name: string): Promise<void> {
  const res = await fetch(`${API_BASE}/system/backups/${encodeURIComponent(name)}`, {
    headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
  });
  await saveBlob(res, `${name}.dump`);
}

/** Override an editable task's schedule and/or toggle it (admin). Pass
 *  `cron: null` to reset to the registry default; omit `cron` to leave the
 *  schedule untouched. Applied by the next scheduler tick (≤1 min). */
export const updateMaintenance = (
  key: string,
  body: { cron?: string | null; enabled?: boolean },
) =>
  request<MaintenanceTask>(`/system/maintenance/${key}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });

/** One component row of the GitHub update check (Jobs page Updates card). */
export type UpdateComponent = {
  component: string;
  running: string;
  latest: string;
  /** null = not determinable on this install (e.g. stampless dev checkout). */
  update_available: boolean | null;
  detail: string;
};

export type UpdateChangelogEntry = {
  sha: string;
  date: string | null;
  subject: string;
  body: string;
  /** True when the commit postdates the running build; null when unknown. */
  is_new: boolean | null;
};

export type UpdateCheck = {
  /** null = no check has run yet (the GET never contacts GitHub by itself). */
  checked_at: string | null;
  source: string;
  error?: string;
  components: UpdateComponent[];
  changelog: UpdateChangelogEntry[];
};

/** Cached update-check state (admin). Contacts GitHub only under the
 *  FILEARR_UPDATE_CHECK_AUTO opt-in when the cache is stale. */
export const getUpdateCheck = () => request<UpdateCheck>("/system/update-check");

/** Operator-initiated "Check now" (admin) — contacts GitHub immediately. */
export const runUpdateCheck = () =>
  request<UpdateCheck>("/system/update-check", { method: "POST" });

/** One optional feature's gate state (Jobs page "Optional features" card).
 *  `scope` says WHERE it is controlled: `"env"` = a process-level variable
 *  (`env`, needs a restart), `"libraries"` = a per-library opt-in, in which
 *  case `count`/`total` report how many libraries have it on. */
export type SystemFeature = {
  key: string;
  title: string;
  enabled: boolean;
  scope: "env" | "libraries";
  env: string | null;
  detail: string;
  /** libraries scope only: libraries with the toggle on / libraries total. */
  count?: number;
  total?: number;
  /** thumbnail_budget only: the effective advisory budget, in GB. */
  value_gb?: number;
};

/** Read-only visibility into which optional features are on (read scope).
 *  Deliberately has no mutating counterpart: env-backed gates need a restart. */
export const systemFeatures = () =>
  request<{ features: SystemFeature[] }>("/system/features");

/** The whole running build stack (About page, read scope). Every version in
 *  the response is read from the live process/database/service — see the
 *  endpoint docstring. The row shapes live in ./about (DOM-free, so the
 *  "unknown value" rendering rules are unit-testable). */
export const systemAbout = () => request<About>("/system/about");

/** One row of the unified app+worker log stream (Jobs page Logs panel). */
export type LogRow = {
  id: number;
  ts: string;
  source: "app" | "worker";
  level: string;
  logger: string;
  message: string;
  /** Length-capped traceback when the record carried one. */
  exc: string | null;
};

export type LogsResponse = {
  /** False when the DB log sink is disabled (explains an empty panel). */
  enabled: boolean;
  logs: LogRow[];
  /** Keyset cursor for the next (older) page; null when this page was short. */
  next_before_id: number | null;
};

/** Tail of the unified log stream, newest first (read scope). */
export const fetchLogs = (
  opts: {
    min_level?: "info" | "warning" | "error" | "critical";
    source?: "app" | "worker";
    q?: string;
    limit?: number;
    before_id?: number;
  } = {},
) => {
  const qs = new URLSearchParams();
  if (opts.min_level) qs.set("min_level", opts.min_level);
  if (opts.source) qs.set("source", opts.source);
  if (opts.q) qs.set("q", opts.q);
  if (opts.limit) qs.set("limit", String(opts.limit));
  if (opts.before_id) qs.set("before_id", String(opts.before_id));
  const s = qs.toString();
  return request<LogsResponse>(`/system/logs${s ? `?${s}` : ""}`);
};

/** Re-prioritise a queue's PENDING (todo) jobs (UI-T14, admin scope). `priority`
 *  is clamped server-side to -100..100; higher runs sooner. Running jobs are
 *  unaffected. Returns the affected row count. */
export const setJobPriority = (queue: string, priority: number) =>
  request<JobPriorityResult>("/system/jobs/priority", {
    method: "POST",
    body: JSON.stringify({ queue, priority, scope: "pending" }),
  });


// ---- UI-T12 in-page folder navigation (browse tree) ----
export interface TreeFolder {
  name: string;
  item_count: number;
}

export interface TreeItem {
  id: string;
  rel_path: string;
  filename: string;
  // W8-B: the taxonomy classification replaced the removed media_type.
  file_category: string | null;
  file_group: string | null;
  size: number;
  title: string | null;
  year: number | null;
}

export interface TreeResponse {
  library_id: string;
  library_name: string;
  path: string;
  folders: TreeFolder[];
  folders_total: number;
  folders_offset: number;
  items: TreeItem[];
  total_items: number;
}

/** Browse a library's folder tree (read scope). `path` is a rel_path ('' = root);
 *  a traversal/absolute path yields a 422 the browse view surfaces inline. */
export const libraryTree = (
  id: string,
  path = "",
  limit = 100,
  offset = 0,
  foldersOffset = 0,
) =>
  request<TreeResponse>(
    `/libraries/${id}/tree?path=${encodeURIComponent(path)}&limit=${limit}&offset=${offset}&folders_offset=${foldersOffset}`,
  );


// ---- P3-T7 saved searches (named, persisted /search queries) ----
export interface SavedSearch {
  id: string;
  name: string;
  /** The flat /search params bundle, stored verbatim and replayed via /search. */
  params: Record<string, string>;
  owner_principal: string | null;
  created_at: string;
  updated_at: string;
}

export const listSavedSearches = () => request<SavedSearch[]>("/saved-searches");

export const createSavedSearch = (body: {
  name: string;
  params: Record<string, unknown>;
  owner_principal?: string | null;
}) => request<SavedSearch>("/saved-searches", { method: "POST", body: JSON.stringify(body) });

// PATCH: rename and/or replace params. An unknown param key is a 422 server-side.
export const updateSavedSearch = (
  id: string,
  patch: Partial<{ name: string; params: Record<string, unknown> }>,
) => request<SavedSearch>(`/saved-searches/${id}`, { method: "PATCH", body: JSON.stringify(patch) });

export async function deleteSavedSearch(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/saved-searches/${id}`, {
    method: "DELETE",
    headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
  });
  if (res.status === 204) return;
  throw new Error(`${res.status}: ${await res.text()}`);
}

// ---- P4-T1 metadata profiles (read-only field vocabulary for key-facts) ----
export interface MetadataProfileField {
  type: string;
  label: string;
  required: boolean;
  facetable: boolean;
  sortable: boolean;
}

export interface MetadataProfile {
  id: string;
  file_category: string;
  version: number;
  created_at: string;
  /** field name -> declared shape (label/type/hints). NOTE: JSONB key order is
   *  not guaranteed to equal the code-declared FieldSpec order (P4-T12 known
   *  limitation) — the key-facts component orders by this map's iteration. */
  fields: Record<string, MetadataProfileField>;
}

/** One profile by media type; 404 for an unknown/unseeded type (callers treat a
 *  failure as "no profile" and fall back to raw key names). */
export const getMetadataProfile = (mediaType: string) =>
  request<MetadataProfile>(`/metadata-profiles/${encodeURIComponent(mediaType)}`);

// --------------------------------------------------------------------------- //
// P8-T12/T13 — alerting: channels, rules, events                              //
// --------------------------------------------------------------------------- //
// Secret sub-fields of a channel config are WRITE-ONLY: a GET returns the
// "__redacted__" marker, and an edit that keeps a secret sends the
// "__unchanged__" sentinel (or omits it). The decrypted value never round-trips.
export const CHANNEL_TYPES = ["webhook", "email", "apprise"] as const;
export type ChannelType = (typeof CHANNEL_TYPES)[number];
export const DISPATCH_LOCALITIES = ["central", "agent"] as const;
export type DispatchLocality = (typeof DISPATCH_LOCALITIES)[number];
export const EVENT_TYPES = ["created", "modified", "deleted", "moved"] as const;
export type AlertEventType = (typeof EVENT_TYPES)[number];
export const DIGEST_WINDOWS = ["hourly", "daily"] as const;
export type DigestWindow = (typeof DIGEST_WINDOWS)[number];

// FIX-16: per-channel webhook payload shape. `generic` is Filearr's native
// signed JSON (back-compat default); `discord`/`slack` reshape the body so those
// endpoints accept it (Discord rejects a body without content/embeds).
export const WEBHOOK_FORMATS = ["generic", "discord", "slack"] as const;
export type WebhookFormat = (typeof WEBHOOK_FORMATS)[number];

/** Auto-detect a webhook format from its URL (new-channel UI default; the
 *  select stays editable). Mirrors the backend `webhook_formats.detect_format`. */
export function detectWebhookFormat(url: string): WebhookFormat {
  try {
    const u = new URL(url);
    const host = u.hostname.toLowerCase();
    const discordHosts = ["discord.com", "discordapp.com"];
    const isDiscordHost =
      discordHosts.includes(host) ||
      discordHosts.some((h) => host.endsWith("." + h));
    if (isDiscordHost && u.pathname.includes("/api/webhooks")) return "discord";
    if (host === "hooks.slack.com") return "slack";
  } catch {
    // not a parseable URL yet — fall through to generic
  }
  return "generic";
}

/** Sentinel a client sends to KEEP an existing (encrypted) channel secret. */
export const UNCHANGED = "__unchanged__";
/** Marker the API returns in place of any stored secret on read. */
export const REDACTED = "__redacted__";

export interface AlertChannel {
  id: string;
  name: string;
  type: ChannelType;
  config: Record<string, unknown>;
  dispatch_locality: DispatchLocality;
  enabled: boolean;
  created_at: string;
}

export const listAlertChannels = () => request<AlertChannel[]>("/alert-channels");

export const createAlertChannel = (body: {
  name: string;
  type: ChannelType;
  config: Record<string, unknown>;
  dispatch_locality?: DispatchLocality;
  enabled?: boolean;
}) => request<AlertChannel>("/alert-channels", { method: "POST", body: JSON.stringify(body) });

export const updateAlertChannel = (
  id: string,
  patch: Partial<{
    name: string;
    config: Record<string, unknown>;
    dispatch_locality: DispatchLocality;
    enabled: boolean;
  }>,
) => request<AlertChannel>(`/alert-channels/${id}`, { method: "PATCH", body: JSON.stringify(patch) });

export async function deleteAlertChannel(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/alert-channels/${id}`, {
    method: "DELETE",
    headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
  });
  if (res.status === 204) return;
  throw new Error(`${res.status}: ${await res.text()}`);
}

export interface TestFireResult {
  ok: boolean;
  detail: string;
  status_code: number | null;
  retryable: boolean;
}

/** Fire a sample alert through the real driver (delivery failures come back in
 *  the 200 body with ok=false; config/secret problems are 4xx/503). */
export const testAlertChannel = (id: string) =>
  request<TestFireResult>(`/alert-channels/${id}/test`, { method: "POST" });

export interface AlertRule {
  id: string;
  name: string;
  enabled: boolean;
  is_system: boolean;
  library_id: string | null;
  path_glob: string | null;
  event_types: string[];
  hash_change_only: boolean;
  group_by: string[];
  group_wait_s: number;
  digest_window: DigestWindow | null;
  repeat_interval_s: number | null;
  threshold_count: number | null;
  threshold_window_s: number | null;
  channel_ids: string[];
  created_at: string;
}

export const listAlertRules = () => request<AlertRule[]>("/alert-rules");

export const createAlertRule = (body: {
  name: string;
  enabled?: boolean;
  library_id?: string | null;
  path_glob?: string | null;
  event_types: string[];
  hash_change_only?: boolean;
  group_wait_s?: number;
  digest_window?: DigestWindow | null;
  repeat_interval_s?: number | null;
  channel_ids?: string[];
}) => request<AlertRule>("/alert-rules", { method: "POST", body: JSON.stringify(body) });

// System rules: only channels + throttle/timings are editable (the match logic
// is read-only). User rules may patch every field below.
export const updateAlertRule = (
  id: string,
  patch: Partial<{
    name: string;
    enabled: boolean;
    library_id: string | null;
    path_glob: string | null;
    event_types: string[];
    hash_change_only: boolean;
    group_wait_s: number;
    digest_window: DigestWindow | null;
    repeat_interval_s: number | null;
    channel_ids: string[];
  }>,
) => request<AlertRule>(`/alert-rules/${id}`, { method: "PATCH", body: JSON.stringify(patch) });

export async function deleteAlertRule(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/alert-rules/${id}`, {
    method: "DELETE",
    headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
  });
  if (res.status === 204) return;
  throw new Error(`${res.status}: ${await res.text()}`);
}

export type AlertEventStatus = "delivered" | "failed" | "pending";

export interface AlertEvent {
  id: string;
  rule_id: string;
  item_id: string | null;
  library_id: string | null;
  event_type: string;
  dedup_key: string;
  status: AlertEventStatus;
  delivered: boolean;
  delivered_at: string | null;
  delivery_attempts: number;
  occurred_at: string;
  last_error: string | null;
}

export const listAlertEvents = (
  filters: { rule_id?: string; library_id?: string; status?: AlertEventStatus; limit?: number } = {},
) => {
  const qs = new URLSearchParams();
  if (filters.rule_id) qs.set("rule_id", filters.rule_id);
  if (filters.library_id) qs.set("library_id", filters.library_id);
  if (filters.status) qs.set("status", filters.status);
  qs.set("limit", String(filters.limit ?? 100));
  return request<AlertEvent[]>(`/alert-events?${qs}`);
};

export interface AlertEventSummary {
  delivered: number;
  failed: number;
  pending: number;
}

export const alertEventsSummary = (library_id?: string) =>
  request<AlertEventSummary>(
    `/alert-events/summary${library_id ? `?library_id=${encodeURIComponent(library_id)}` : ""}`,
  );

// ---- P3-T10 duplicate awareness (copy counts + copy listing) ----
export interface ItemCopy {
  id: string;
  library_id: string;
  library_name: string | null;
  rel_path: string;
  path: string;
  native_path: string | null;
  size: number;
  last_seen: string;
}

export interface CopiesResponse {
  id: string;
  /** Full group size INCLUDING this item — the badge reads "N copies". */
  count: number;
  /** Which key grouped the copies: "content_hash" | "quick_hash" | "none". */
  match: string;
  capped: boolean;
  copies: ItemCopy[];
}

/** The OTHER active copies of an item (read scope). Used by the ItemDetail
 *  Copies section; `count` is the full group size incl. self. */
export const itemCopies = (id: string) =>
  request<CopiesResponse>(`/items/${id}/copies`);

/** Batch copy-count badges for a page of results (read scope). Body is up to 200
 *  ids; the response maps id -> count ONLY for groups with more than one member.
 *  A single grouped SQL pass — never a per-row query. */
export const copyCounts = (ids: string[]) =>
  request<Record<string, number>>("/items/copy-counts", {
    method: "POST",
    body: JSON.stringify({ ids }),
  });

// ---- P3-T12 tag facet type-ahead ----
export interface TagSuggestion {
  value: string;
  count: number;
}

/** Typo-tolerant, count-ordered tag suggestions from Meili facet-search (read
 *  scope). `q` is the partial tag; the optional scope narrows the counts. */
export function searchTags(
  q: string,
  scope: { type?: string; library?: string } = {},
  signal?: AbortSignal,
): Promise<{ tags: TagSuggestion[] }> {
  const params: Record<string, string> = { q };
  if (scope.type) params.type = scope.type;
  if (scope.library) params.library = scope.library;
  const qs = new URLSearchParams(params);
  return request(`/search/tags?${qs}`, { signal });
}

// ---- P3-T14 timeline (date histogram over mtime) ----
export interface TimelineBucket {
  start: string;
  start_epoch: number;
  end_epoch: number;
  count: number;
}

export interface TimelineResponse {
  bucket: "month" | "year";
  library: string | null;
  buckets: TimelineBucket[];
  invalid_count: number;
  /** mtime_gte value the UI uses to filter the "invalid dates" (future) bucket. */
  invalid_mtime_gte: number;
}

/** Date histogram of active items by mtime (read scope). `bucket` is month|year;
 *  `library` scopes to one library. */
export const timeline = (bucket: "month" | "year" = "month", library = "") => {
  const qs = new URLSearchParams({ bucket });
  if (library) qs.set("library", library);
  return request<TimelineResponse>(`/stats/timeline?${qs}`);
};

export interface LibraryStatsRow {
  library_id: string;
  name: string;
  is_agent: boolean;
  file_count: number;
  total_bytes: number;
  sidecar_count: number;
  missing_count: number;
  trashed_count: number;
}

export interface LibraryStatsResponse {
  libraries: LibraryStatsRow[];
  total_files: number;
  total_bytes: number;
}

/** Per-library catalog footprint (active file count + bytes, tombstone tails)
 *  plus catalog-wide totals. One grouped aggregate — cheap, but run on demand
 *  (the Admin overview), not on every library-dropdown load. */
export const libraryStats = () => request<LibraryStatsResponse>("/stats/libraries");

// ---- P11 reporting v1 ----
export type RowLink = "item" | "search_ext" | "search_hash" | "browse" | "none";

export interface ReportMeta {
  id: string;
  title: string;
  description: string;
  columns: string[];
  supports_library: boolean;
  is_capped: boolean;
  default_limit: number;
  /** How the UI makes a row interactive (P11 polish). */
  row_link: RowLink;
  // IN-T2: one generic numeric parameter slot. A report DECLARES that it takes
  // a day threshold (``stale_files`` does; ``bad_mtime``'s 48h stays hardcoded),
  // and the UI renders the input only for those — no per-report special-casing
  // in the page. Older backends omit these keys, hence the optional types.
  supports_threshold?: boolean;
  threshold_label?: string;
  default_threshold_days?: number;
}

/** The streaming machine-readable export formats (JSON stays the paginated
 *  envelope). Drives the Download dropdown + per-format file extension. */
export type ExportFormat = "csv" | "ndjson" | "xml" | "xlsx";
export const EXPORT_FORMATS: ExportFormat[] = ["csv", "ndjson", "xml", "xlsx"];

export interface ReportPage {
  report: ReportMeta;
  columns: string[];
  rows: Record<string, unknown>[];
  limit: number;
  offset: number;
  count: number;
  has_more: boolean;
}

/** The canned-report registry (metadata only). */
export const listReports = () =>
  request<{ reports: ReportMeta[] }>("/reports").then((r) => r.reports);

/** The parameters every report-running path accepts. ``thresholdDays`` (IN-T2)
 *  is only meaningful for a report whose meta declares ``supports_threshold``;
 *  it is dropped from the query when absent, so nothing changes for the rest. */
export interface ReportRunOpts {
  limit?: number;
  offset?: number;
  libraryId?: string;
  thresholdDays?: number;
}

/** Build the query string shared by the JSON-page and CSV-download paths. */
function reportQuery(opts: ReportRunOpts): string {
  const qs = new URLSearchParams();
  if (opts.limit != null) qs.set("limit", String(opts.limit));
  if (opts.offset != null) qs.set("offset", String(opts.offset));
  if (opts.libraryId) qs.set("library_id", opts.libraryId);
  if (opts.thresholdDays != null) qs.set("threshold_days", String(opts.thresholdDays));
  const s = qs.toString();
  return s ? `?${s}` : "";
}

/** Run a canned report and return one JSON page. */
export const runReport = (id: string, opts: ReportRunOpts = {}) =>
  request<ReportPage>(`/reports/${id}${reportQuery(opts)}`);

// ---- IN-T3 folder tree (treemap drill-down) ---------------------------------
/** One direct child of the requested folder. ``name`` is the single path segment
 *  (or the reserved ``"."`` bucket for files sitting DIRECTLY in the parent);
 *  ``folder`` is the full rel-path of that child. */
export interface FolderTreeChild {
  name: string;
  folder: string;
  library_id: string;
  library: string;
  file_count: number;
  total_bytes: number;
  /** Whether anything lives deeper than one segment below — drives whether the
   *  cell offers a drill affordance at all. */
  has_children: boolean;
}

export interface FolderTreeResponse {
  parent: string;
  library_id: string | null;
  children: FolderTreeChild[];
  /** More children exist than ``limit`` returned (largest-first, so the tail is
   *  the small stuff). The UI says so rather than implying a complete picture. */
  truncated: boolean;
}

/** ``GET /reports/folder-tree`` — the direct children of one folder, ONE level
 *  at a time. Deliberately not a canned report: a treemap drills, and each drill
 *  is a small independent query. With no ``libraryId`` the root level returns one
 *  child per LIBRARY; every deeper call pins the library from the child row. */
export const folderTree = (
  opts: { libraryId?: string; parent?: string; limit?: number } = {},
) => {
  const qs = new URLSearchParams();
  if (opts.libraryId) qs.set("library_id", opts.libraryId);
  if (opts.parent) qs.set("parent", opts.parent);
  if (opts.limit != null) qs.set("limit", String(opts.limit));
  const s = qs.toString();
  return request<FolderTreeResponse>(`/reports/folder-tree${s ? `?${s}` : ""}`);
};

/** Save a streamed fetch Response body to a download file. Central because a
 *  bare <a download> link can't carry the Bearer auth header, so every export
 *  goes fetch -> blob -> object URL -> synthetic click. */
async function saveBlob(res: Response, filename: string): Promise<void> {
  if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function stampToday(): string {
  return new Date().toISOString().slice(0, 10).replace(/-/g, "");
}

/** Download a canned report in a chosen streaming format (csv/ndjson/xml). Uses
 *  fetch (auth header) + a blob so the streamed body is saved, not rendered. */
export async function downloadReport(
  id: string,
  format: ExportFormat,
  opts: ReportRunOpts = {},
): Promise<void> {
  const q = reportQuery({ ...opts, offset: undefined });
  const sep = q ? "&" : "?";
  const res = await fetch(`${API_BASE}/reports/${id}${q}${sep}format=${format}`, {
    headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
  });
  await saveBlob(res, `filearr-${id}-${stampToday()}.${format}`);
}

// --------------------------------------------------------------------------- //
// P11-T5/T9/T11 — background export JOBS + scheduled report delivery.          //
// --------------------------------------------------------------------------- //
export type ExportStatus = "queued" | "running" | "complete" | "failed";

export interface ReportExport {
  id: string;
  status: ExportStatus;
  format: ExportFormat;
  canned_report_key: string | null;
  report_definition_id: string | null;
  triggered_by: string;
  row_count: number | null;
  file_size_bytes: number | null;
  error: string | null;
  delivery_status: string | null;
  created_at: string | null;
  finished_at: string | null;
  expires_at: string | null;
  purged_at: string | null;
  downloadable: boolean;
}

/** Queue a background export of a canned report. ``thresholdDays`` rides through
 *  ``export.params`` so a scheduled/background run reproduces exactly what the
 *  operator saw on screen (IN-T2). */
export const enqueueReportExport = (
  id: string,
  format: ExportFormat,
  opts: ReportRunOpts = {},
) => {
  const qs = new URLSearchParams({ format });
  if (opts.limit != null) qs.set("limit", String(opts.limit));
  if (opts.libraryId) qs.set("library_id", opts.libraryId);
  if (opts.thresholdDays != null) qs.set("threshold_days", String(opts.thresholdDays));
  return request<ReportExport>(`/reports/${id}/export?${qs.toString()}`, {
    method: "POST",
  });
};

/** Queue a background export of a custom report. */
export const enqueueCustomReportExport = (
  id: string,
  format: ExportFormat,
  opts: { limit?: number } = {},
) => {
  const qs = new URLSearchParams({ format });
  if (opts.limit != null) qs.set("limit", String(opts.limit));
  return request<ReportExport>(`/custom-reports/${id}/export?${qs.toString()}`, {
    method: "POST",
  });
};

export const listExports = () =>
  request<{ exports: ReportExport[] }>("/exports").then((r) => r.exports);

export const getExport = (id: string) => request<ReportExport>(`/exports/${id}`);

/** Fetch a finished export artifact (auth header) and save it as a file. */
export async function downloadExport(ex: ReportExport): Promise<void> {
  const res = await fetch(`${API_BASE}/exports/${ex.id}/download`, {
    headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
  });
  await saveBlob(res, `filearr-export-${ex.id}.${ex.format}`);
}

// ---- Scheduled reports (P11-T9) ----
export interface ReportSchedule {
  id: string;
  name: string;
  owner_principal: string | null;
  canned_report_key: string | null;
  report_definition_id: string | null;
  params: Record<string, unknown>;
  format: ExportFormat;
  cron: string;
  channel_id: string | null;
  enabled: boolean;
  last_cron_fired_at: string | null;
  created_at: string;
  updated_at: string;
}

export const listReportSchedules = () =>
  request<ReportSchedule[]>("/report-schedules");

export const createReportSchedule = (body: {
  name: string;
  canned_report_key?: string | null;
  report_definition_id?: string | null;
  params?: Record<string, unknown>;
  format: ExportFormat;
  cron: string;
  channel_id?: string | null;
  enabled?: boolean;
}) =>
  request<ReportSchedule>("/report-schedules", {
    method: "POST",
    body: JSON.stringify(body),
  });

export const updateReportSchedule = (
  id: string,
  patch: Partial<{
    name: string;
    params: Record<string, unknown>;
    format: ExportFormat;
    cron: string;
    channel_id: string | null;
    enabled: boolean;
  }>,
) =>
  request<ReportSchedule>(`/report-schedules/${id}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });

export async function deleteReportSchedule(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/report-schedules/${id}`, {
    method: "DELETE",
    headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
  });
  if (res.status === 204) return;
  throw new Error(`${res.status}: ${await res.text()}`);
}

// --------------------------------------------------------------------------- //
// Phase 6 (P6-T1) — local accounts + sessions.                                //
// The session cookie (filearr_session, HttpOnly) rides along automatically on  //
// same-origin fetches; these helpers never touch the token. `authStatus` is    //
// the public probe that tells the SPA whether to show a login wall.            //
// --------------------------------------------------------------------------- //
export type AuthMode = "disabled" | "bootstrap" | "enabled";

export interface AuthStatus {
  auth_enabled: boolean;
  users_exist: boolean;
  mode: AuthMode;
  // P6-T5: when true the login page shows a "Sign in with SSO" button.
  oidc_enabled: boolean;
}

/** Full-page navigation target that starts the OIDC redirect flow. This is a
 *  top-level browser navigation (NOT a fetch) — the IdP round-trip and the
 *  Set-Cookie on return require a real navigation. ``returnTo`` (a local path)
 *  is where the callback sends the browser after a successful login. */
export function oidcLoginUrl(returnTo = "/"): string {
  const q = new URLSearchParams({ return_to: returnTo }).toString();
  return `${API_BASE}/auth/oidc/login?${q}`;
}

export interface AuthPrincipal {
  id: string;
  username: string;
  email: string | null;
  /** A role NAME: the builtins admin/user/viewer or an operator-defined role. */
  global_role: string;
  kind: string;
  disabled: boolean;
  // P6-T10/T12: identity source ('local'|'ldap'|'saml'|'oidc'). Older payloads
  // may omit it (defaults 'local' server-side).
  auth_provider?: string;
  // Self-service profile + preferences (2026-08-16).
  display_name?: string | null;
  phone?: string | null;
  preferences?: Record<string, unknown>;
  // Per-user session-timeout overrides in hours (null = global applies).
  session_inactivity_hours?: number | null;
  session_ttl_hours?: number | null;
  /** Coarse API scopes the role grants; gate admin UI on `scopes.includes("admin")`. */
  scopes?: string[];
}

/** True when the principal's role carries the admin scope (works for custom
 *  roles too — never test `global_role === "admin"` in the UI). */
export const isAdminPrincipal = (me: AuthPrincipal | null | undefined): boolean =>
  !!me && (me.scopes ? me.scopes.includes("admin") : me.global_role === "admin");

// --- Account (self-service) --------------------------------------------------
export interface ProfilePatch {
  username?: string;
  display_name?: string;
  email?: string;
  phone?: string;
}
export const patchMyProfile = (body: ProfilePatch) =>
  request<AuthPrincipal>("/auth/me", { method: "PATCH", body: JSON.stringify(body) });
export const putMyPreferences = (prefs: Record<string, unknown>) =>
  request<AuthPrincipal>("/auth/me/preferences", { method: "PUT", body: JSON.stringify(prefs) });
export const changeMyPassword = (current_password: string, new_password: string) =>
  request<unknown>("/auth/password", {
    method: "POST",
    body: JSON.stringify({ current_password, new_password }),
  });
export interface MySessionTimeouts {
  inactivity_hours: number;
  ttl_hours: number;
  inactivity_source: "env" | "global" | "user";
  ttl_source: "env" | "global" | "user";
}
export const mySessionTimeouts = () => request<MySessionTimeouts>("/auth/me/session-timeouts");

// --- Global session-timeout settings (admin, runtime) -------------------------
export interface SessionSettings {
  inactivity_hours: number;
  ttl_hours: number;
  inactivity_source: "env" | "global";
  ttl_source: "env" | "global";
  env_inactivity_hours: number;
  env_ttl_hours: number;
  min_hours: number;
  max_hours: number;
}
export const getSessionSettings = () => request<SessionSettings>("/auth/settings/session");
/** 0 clears an override back to the env default; omit a field to leave it. */
export const patchSessionSettings = (body: { inactivity_hours?: number; ttl_hours?: number }) =>
  request<SessionSettings>("/auth/settings/session", { method: "PATCH", body: JSON.stringify(body) });

// --- Roles (data since 2026-08-16) -------------------------------------------
export interface RoleDef {
  name: string;
  display_name: string;
  description: string;
  builtin: boolean;
  scopes: string[];
  ceiling_actions: string[];
  bypass: boolean;
  users: number;
  updated_at?: string | null;
}
export interface RoleCompare {
  scopes: string[];
  actions: string[];
  action_help: Record<string, string>;
  scope_help: Record<string, string>;
  roles: RoleDef[];
  /** role name -> { "scope:<s>" | "action:<a>" -> boolean } */
  matrix: Record<string, Record<string, boolean>>;
  users_by_role: Record<string, string[]>;
}
export const listRoles = () => request<RoleDef[]>("/rbac/roles");
export const compareRoles = () => request<RoleCompare>("/rbac/roles/compare");
export const createRole = (body: {
  name: string;
  display_name: string;
  description?: string;
  scopes?: string[];
  ceiling_actions?: string[];
  clone_from?: string;
}) => request<RoleDef>("/rbac/roles", { method: "POST", body: JSON.stringify(body) });
export const patchRole = (
  name: string,
  body: { display_name?: string; description?: string; scopes?: string[]; ceiling_actions?: string[] },
) => request<RoleDef>(`/rbac/roles/${encodeURIComponent(name)}`, { method: "PATCH", body: JSON.stringify(body) });
export async function deleteRole(name: string): Promise<void> {
  const res = await fetch(`${API_BASE}/rbac/roles/${encodeURIComponent(name)}`, {
    method: "DELETE",
    credentials: "same-origin",
    headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
  });
  if (!res.ok) throw new ApiError(res.status, await res.text());
}

export interface LoginResult {
  principal: AuthPrincipal;
  warning: string | null;
}

export const authStatus = () => request<AuthStatus>("/auth/status");

/** Current session principal, or null when not authenticated (401). */
export async function authMe(): Promise<AuthPrincipal | null> {
  const res = await fetch(`${API_BASE}/auth/me`, { credentials: "same-origin" });
  if (res.status === 401) return null;
  if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`);
  return res.json() as Promise<AuthPrincipal>;
}

export function authLogin(username: string, password: string): Promise<LoginResult> {
  return request<LoginResult>("/auth/login", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
}

export function authBootstrap(username: string, password: string): Promise<AuthPrincipal> {
  return request<AuthPrincipal>("/auth/bootstrap", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
}

export async function authLogout(): Promise<void> {
  await fetch(`${API_BASE}/auth/logout`, { method: "POST", credentials: "same-origin" });
}

// --------------------------------------------------------------------------- //
// P6-T2 — RBAC: users, groups, path grants, decision preview (admin scope).   //
// --------------------------------------------------------------------------- //
export interface RbacGroup {
  id: string;
  name: string;
  description: string | null;
  source: string;
  member_count: number;
}
export interface RbacMember {
  principal_id: string;
  username: string | null;
  global_role: string | null;
}
export interface RbacGrant {
  id: string;
  subject_kind: "principal" | "group";
  subject_id: string;
  subject_label: string | null;
  library_id: string;
  scope: string;
  action: string;
  effect: "allow" | "deny";
}
export interface RbacActions {
  actions: string[];
  role_ceilings: Record<string, string[]>;
}
export interface RbacDecision {
  allowed: boolean;
  reason: string;
  action: string;
  role: string;
  item_scope: string;
  winning_grant: {
    scope: string;
    action: string;
    effect: string;
    subject_kind: string | null;
    subject_id: string | null;
  } | null;
}

export const listRbacActions = () => request<RbacActions>("/rbac/actions");
export const listUsers = () => request<AuthPrincipal[]>("/auth/users");

export const listGroups = () => request<RbacGroup[]>("/rbac/groups");
export const createGroup = (name: string, description: string | null) =>
  request<RbacGroup>("/rbac/groups", {
    method: "POST",
    body: JSON.stringify({ name, description }),
  });
export async function deleteGroup(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/rbac/groups/${id}`, {
    method: "DELETE",
    headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
  });
  if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`);
}
export const listMembers = (groupId: string) =>
  request<RbacMember[]>(`/rbac/groups/${groupId}/members`);
export async function addMember(groupId: string, principalId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/rbac/groups/${groupId}/members`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}),
    },
    body: JSON.stringify({ principal_id: principalId }),
  });
  if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`);
}
export async function removeMember(groupId: string, principalId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/rbac/groups/${groupId}/members/${principalId}`, {
    method: "DELETE",
    headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
  });
  if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`);
}

export const listGrants = () => request<RbacGrant[]>("/rbac/grants");
export const createGrant = (body: {
  subject_kind: "principal" | "group";
  subject_id: string;
  library_id: string;
  rel_path: string;
  action: string;
  effect: "allow" | "deny";
}) =>
  request<RbacGrant>("/rbac/grants", {
    method: "POST",
    body: JSON.stringify(body),
  });
export async function deleteGrant(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/rbac/grants/${id}`, {
    method: "DELETE",
    headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
  });
  if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`);
}

export const rbacPreview = (
  principal: string,
  library: string,
  path: string,
  action: string,
) =>
  request<RbacDecision>(
    `/rbac/preview?principal=${encodeURIComponent(principal)}` +
      `&library=${encodeURIComponent(library)}` +
      `&path=${encodeURIComponent(path)}` +
      `&action=${encodeURIComponent(action)}`,
  );

// ---- P11 custom (saved-query) reports ----
export interface ReportDefinition {
  id: string;
  name: string;
  owner_principal: string | null;
  query: string;
  columns: string[];
  sort: string | null;
  format: string;
  created_at: string;
  updated_at: string;
}

export interface ReportValidationError {
  error: string;
  code?: string;
  position?: number;
  reason?: string;
  message?: string;
  unsupported?: string[];
}

export interface CustomRunPage {
  report: { id: string; name: string; columns: string[] };
  columns: string[];
  rows: Record<string, unknown>[];
  limit: number;
  offset: number;
  count: number;
  has_more: boolean;
}

export interface ColumnRegistry {
  core: string[];
  custom_fields: string[];
  formats: string[];
}

export const listCustomReports = () =>
  request<ReportDefinition[]>("/custom-reports");

export const getColumnRegistry = () =>
  request<ColumnRegistry>("/custom-reports/columns");

export const validateCustomReport = (body: {
  query: string;
  columns: string[];
  sort?: string | null;
}) =>
  request<{ ok: boolean; errors: ReportValidationError[] }>(
    "/custom-reports/validate",
    { method: "POST", body: JSON.stringify(body) },
  );

export const createCustomReport = (body: {
  name: string;
  query: string;
  columns: string[];
  sort?: string | null;
  format?: string;
}) =>
  request<ReportDefinition>("/custom-reports", {
    method: "POST",
    body: JSON.stringify(body),
  });

export const updateCustomReport = (
  id: string,
  body: Partial<{
    name: string;
    query: string;
    columns: string[];
    sort: string | null;
    format: string;
  }>,
) =>
  request<ReportDefinition>(`/custom-reports/${id}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });

export async function deleteCustomReport(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/custom-reports/${id}`, {
    method: "DELETE",
    headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
  });
  if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`);
}

export const runCustomReport = (
  id: string,
  opts: { limit?: number; offset?: number } = {},
) => {
  const qs = new URLSearchParams({ format: "json" });
  if (opts.limit != null) qs.set("limit", String(opts.limit));
  if (opts.offset != null) qs.set("offset", String(opts.offset));
  return request<CustomRunPage>(`/custom-reports/${id}/run?${qs.toString()}`);
};

/** Download a custom report in a chosen streaming format (csv/ndjson/xml). */
export async function downloadCustomReport(
  id: string,
  name: string,
  format: ExportFormat,
): Promise<void> {
  const res = await fetch(`${API_BASE}/custom-reports/${id}/run?format=${format}`, {
    headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
  });
  // The server sets a safe Content-Disposition filename; the client name is only
  // a friendly default (sanitised for the filesystem).
  const safe = name.replace(/[^\w.-]+/g, "_") || "report";
  await saveBlob(res, `filearr-${safe}-${stampToday()}.${format}`);
}

// --------------------------------------------------------------------------- //
// P6-T8/T9/T11/T12 — security hardening: users, sessions, audit.              //
// --------------------------------------------------------------------------- //

// ---- User management (admin) ----
export const createUser = (body: {
  username: string;
  password: string;
  /** A role name (builtin or custom — see listRoles). */
  global_role?: string;
  email?: string | null;
}) => request<AuthPrincipal>("/auth/users", { method: "POST", body: JSON.stringify(body) });

export const updateUser = (
  id: string,
  patch: Partial<{
    global_role: string;
    disabled: boolean;
    email: string | null;
    password: string;
    display_name: string;
    phone: string;
    /** Hours; 0 clears the per-user override back to the global setting. */
    session_inactivity_hours: number;
    session_ttl_hours: number;
  }>,
) => request<AuthPrincipal>(`/auth/users/${id}`, { method: "PATCH", body: JSON.stringify(patch) });

export async function deleteUser(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/auth/users/${id}`, {
    method: "DELETE",
    headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
    credentials: "same-origin",
  });
  if (res.status === 204) return;
  throw new ApiError(res.status, await res.text());
}

// ---- Active sessions (P6-T11) ----
export interface AuthSession {
  id: string;
  ip_address: string | null;
  user_agent: string | null;
  created_at: string;
  last_seen_at: string;
  current: boolean;
}

export const listMySessions = () => request<AuthSession[]>("/auth/sessions");

export async function revokeMySession(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/auth/sessions/${id}`, {
    method: "DELETE",
    credentials: "same-origin",
    headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
  });
  if (res.status === 204) return;
  throw new ApiError(res.status, await res.text());
}

export async function revokeAllMySessions(): Promise<void> {
  const res = await fetch(`${API_BASE}/auth/sessions/revoke-all`, {
    method: "POST",
    credentials: "same-origin",
    headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
  });
  if (res.status === 204) return;
  throw new ApiError(res.status, await res.text());
}

export const listUserSessions = (principalId: string) =>
  request<AuthSession[]>(`/auth/users/${principalId}/sessions`);

export async function revokeUserSessions(principalId: string): Promise<void> {
  const res = await fetch(`${API_BASE}/auth/users/${principalId}/sessions`, {
    method: "DELETE",
    credentials: "same-origin",
    headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
  });
  if (res.status === 204) return;
  throw new ApiError(res.status, await res.text());
}

// ---- Security audit feed (P6-T9, admin) ----
export interface SecurityEvent {
  id: string;
  event_type: string;
  principal_id: string | null;
  username_attempted: string | null;
  ip: string | null;
  user_agent: string | null;
  ts: string;
  details: Record<string, unknown> | null;
}

export interface AuditPage {
  events: SecurityEvent[];
  next_cursor: string | null;
}

export const listAudit = (
  filters: {
    event_type?: string;
    principal_id?: string;
    since?: string;
    until?: string;
    cursor?: string;
    limit?: number;
  } = {},
) => {
  const qs = new URLSearchParams();
  if (filters.event_type) qs.set("event_type", filters.event_type);
  if (filters.principal_id) qs.set("principal_id", filters.principal_id);
  if (filters.since) qs.set("since", filters.since);
  if (filters.until) qs.set("until", filters.until);
  if (filters.cursor) qs.set("cursor", filters.cursor);
  qs.set("limit", String(filters.limit ?? 50));
  return request<AuditPage>(`/audit?${qs}`);
};

// --------------------------------------------------------------------------- //
// Visual filter builder (user-requested) — live preview + key vocabulary.     //
// --------------------------------------------------------------------------- //
export interface QueryPreviewResponse {
  columns: string[];
  rows: Record<string, unknown>[];
  limit: number;
  offset: number;
  count: number;
  has_more: boolean;
  /** Match count, capped at the server ceiling (10k). */
  total: number;
  /** True when the real match count exceeds the ceiling (render "total+"). */
  total_capped: boolean;
}

/** Reuses the SAME structured validation-error shape as custom reports. */
export type QueryPreviewError = ReportValidationError;

export interface MetaKeyInfo {
  key: string;
  label: string;
  data_type: string;
  file_categories: string[];
}

export interface CustomFieldKeyInfo {
  name: string;
  label: string;
  data_type: string;
  select_options: string[] | null;
}

export interface QueryKeys {
  meta_keys: MetaKeyInfo[];
  custom_fields: CustomFieldKeyInfo[];
  kinds: string[];
  groups: string[];
  source: string;
}

/** Live-preview a querydsl string against real data (read scope, RBAC-scoped).
 *  On a parse/translation error the server returns 422 with the same structured
 *  `{ detail: { validation: [...] } }` body the reports validate/run paths use. */
export async function previewQuery(
  body: { query: string; limit?: number; offset?: number },
  signal?: AbortSignal,
): Promise<QueryPreviewResponse> {
  const res = await fetch(`${API_BASE}/query/preview`, {
    method: "POST",
    signal,
    headers: {
      "Content-Type": "application/json",
      ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}),
    },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new ApiError(res.status, await res.text());
  return res.json() as Promise<QueryPreviewResponse>;
}

/** Extract the structured validation errors from a failed previewQuery (422). */
export function previewValidationErrors(e: unknown): QueryPreviewError[] | null {
  if (!(e instanceof ApiError) || e.status !== 422) return null;
  try {
    const body = JSON.parse(e.body);
    const v = body?.detail?.validation;
    return Array.isArray(v) ? (v as QueryPreviewError[]) : null;
  } catch {
    return null;
  }
}

export const queryKeys = () => request<QueryKeys>("/query/keys");

export interface AssistResponse {
  dsl: string;
  source: "heuristic" | "ollama";
  filters: string[];
  terms: string[];
  notes: string[];
  llm_available: boolean;
}

/** Natural-language → filter-DSL translation. The returned dsl is always
 *  grammar-valid (server re-parses before returning). */
export const assistQuery = (text: string) =>
  request<AssistResponse>("/query/assist", {
    method: "POST",
    body: JSON.stringify({ text }),
  });


// --------------------------------------------------------------------------- //
// P5-T1 — distributed-agent enrollment (admin scope). Mint single-use tokens,  //
// list/revoke tokens + agents. The raw token is shown ONCE by the mint call.   //
// --------------------------------------------------------------------------- //
export type AgentStatus = "pending" | "active" | "revoked";

export interface EnrollmentTokenOut {
  token_hash: string;
  /** P13: the configuration groups (by NAME) the enrolling agent joins. A name
   *  list rather than ids because a token is minted before the agent exists and
   *  is often pasted into an installer by hand. `[]` = Global membership only. */
  config_group_names: string[];
  expires_at: string;
  consumed_at: string | null;
  consumed_by: string | null;
  created_at: string;
  status: "active" | "consumed" | "expired";
}

export interface EnrollmentTokenMint extends EnrollmentTokenOut {
  token: string; // raw, show-once
}

export interface AgentOut {
  id: string;
  name: string;
  hostname: string;
  platform: string;
  status: AgentStatus;
  cert_fingerprint: string | null;
  last_contiguous_seq_no: number;
  last_seen_at: string | null;
  agent_version: string | null;
  /** P13: the config GENERATION (max version seq over the groups that composed
   *  this agent's document) the agent last echoed back via `?applied=`. Compare
   *  against `EffectiveConfigOut.generation` to see whether it has caught up.
   *  Replaces the old whole-document applied-version number, which counted on a
   *  different scale entirely. */
  config_generation_applied: number | null;
  revoked_at: string | null;
  created_at: string;
  /** P13: EXPLICIT configuration-group memberships. Global is implicit and is
   *  never listed here. Populated only by `GET /agents` — a PATCH/DELETE
   *  response always carries `[]`, so never write it back from one. */
  config_group_ids: string[];
  // W6-D3: capability advertisement persisted from the agent's command poll.
  capabilities: Record<string, unknown> | null;
  /** 2026-08-11: central's verdict on each advertised host tool — `ok` /
   *  `outdated` / `unknown` / `absent`, keyed by tool name. Derived from
   *  `capabilities` per response (the minimums are a central opinion, revisable
   *  without touching a fleet of agents), so the console renders a judgement it
   *  does not have to make. `{}` for an agent that has never advertised.
   *  See ./hostTools for how a chip reads it. */
  tool_verdicts: Record<string, string>;
  /** Self-reported health snapshot from the agent's command poll (uptime,
   *  outbox backlog, index size, scan state) + when it arrived. */
  health: Record<string, unknown> | null;
  health_at: string | null;
  /** CENTRAL's observation of the last authenticated request's transport
   *  ('bearer' | 'mtls') — the honest answer to "is this agent on mTLS". */
  last_auth_mode: "bearer" | "mtls" | null;
  // 2026-08-05 update surfacing (list endpoint only): the version this agent
  // would be offered, whether that makes an update available, and whether a
  // self_update command is already in flight. Drives the badge + button.
  update_available: boolean;
  update_target: string | null;
  update_pending: boolean;
  /** Why central is not offering the update on the agent's own poll right now
   *  (auto_update off / update_not_before / outside update_window); null = not held. */
  update_hold?: string | null;
}

/** Paginated registered-agents listing — a fleet can reach hundreds or
 *  thousands of agents, so the console pages server-side (limit capped 200). */
export interface AgentPage {
  items: AgentOut[];
  total: number;
  limit: number;
  offset: number;
}

export const listAgents = (limit = 50, offset = 0) =>
  request<AgentPage>(`/agents?limit=${limit}&offset=${offset}`);

/** PATCH /agents/{id} — the operator-mutable fields of an ENROLLED agent.
 *
 *  Exactly one field since P13: group membership moved to its own full-replace
 *  endpoint (`setAgentConfigGroups`) because an agent is now in MANY groups, and
 *  the old single group-name field went with the whole policy-scope scheme. */
export interface AgentPatchIn {
  name?: string;
}

export const updateAgent = (id: string, body: AgentPatchIn) =>
  request<AgentOut>(`/agents/${id}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });

export type { AgentPolicyDoc };

export const listEnrollmentTokens = () =>
  request<EnrollmentTokenOut[]>("/agents/enrollment-tokens");

/** Mint a single-use enrollment token. `config_group_names` are joined at
 *  register time; an unknown name is fail-safe (the agent enrolls into Global
 *  only), so a typo never blocks an enrollment. */
export const mintEnrollmentToken = (
  config_group_names: string[],
  ttl_minutes?: number,
) =>
  request<{
    token: string;
    token_hash: string;
    config_group_names: string[];
    expires_at: string;
  }>("/agents/enrollment-tokens", {
    method: "POST",
    body: JSON.stringify({
      config_group_names,
      ...(ttl_minutes ? { ttl_minutes } : {}),
    }),
  });

/** Delete an enrollment token. Unconsumed tokens delete freely; a consumed
 *  token's row (which carries the consumed_by link) needs `force` — the audit
 *  event preserves the link before the row goes. */
export async function revokeEnrollmentToken(tokenHash: string, force = false): Promise<void> {
  const res = await fetch(
    `${API_BASE}/agents/enrollment-tokens/${encodeURIComponent(tokenHash)}${force ? "?force=true" : ""}`,
    {
      method: "DELETE",
      credentials: "same-origin",
      headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
    },
  );
  if (!res.ok && res.status !== 204) throw new ApiError(res.status, await res.text());
}

/** Revoke = application-layer denylist (row retained, history kept). */
export const revokeAgent = (id: string) =>
  request<AgentOut>(`/agents/${id}`, { method: "DELETE" });

/** Queue a self_update command for one agent — applied at its next command
 *  check-in (default 60s poll). 409 when already up to date or one is queued. */
export interface SelfUpdateOut {
  command_id: string;
  agent_id: string;
  target: string;
  expires_at: string;
}
export const triggerAgentUpdate = (id: string) =>
  request<SelfUpdateOut>(`/agents/${id}/self-update`, { method: "POST" });

/** Queue a `suspend` command: pause (or resume) the agent's own scan
 *  scheduling + replication push. Applied at its next command poll; the
 *  applied truth then shows up in `health.suspended`. */
export const suspendAgent = (id: string, suspended: boolean) =>
  request<AgentCommandOut>(`/agents/${id}/suspend`, {
    method: "POST",
    body: JSON.stringify({ suspended }),
  });

/** Queue an `agent_maintenance` command: local index VACUUM, outbox prune,
 *  temp-file sweep on the agent. 409 while one is already queued/running. */
export const runAgentMaintenance = (id: string) =>
  request<AgentCommandOut>(`/agents/${id}/maintenance`, { method: "POST" });

/** Queue a `reextract` command (extraction parity phase 3): the agent sweeps
 *  its EXISTING local index, re-runs extraction over items that never got one
 *  (catalogued before `extract_enabled`, or before the host gained ffprobe/
 *  exiftool/poppler/tesseract) and re-emits them through replication. The sweep
 *  is resumable and short-circuits at an unchanged extraction configuration —
 *  `force` re-sweeps anyway; `max_items` bounds one run (omit = everything).
 *  409 while a sweep is already queued or running for that agent. */
export const reextractAgent = (
  id: string,
  opts: { force?: boolean; max_items?: number } = {},
) =>
  request<AgentCommandOut>(`/agents/${id}/reextract`, {
    method: "POST",
    body: JSON.stringify(opts),
  });

/** Queue a `rehash_sweep` command (QH-T6): the agent re-reads every file in its
 *  index inside a size band, recomputes both hashes under the post-QH-T1 rules,
 *  and re-emits only the rows whose stored value was wrong.
 *
 *  NOT the `rehash_check` command kind, which verifies ONE item and writes
 *  nothing. This one runs for hours and rewrites rows.
 *
 *  Why it exists: until 2026-07-18 the hashers read a fixed 64 KiB head and
 *  added the tail only above 128 KiB, so a 64-128 KiB file had its middle and
 *  tail unhashed — false duplicates. The agent's scan only re-hashes files whose
 *  size or mtime moved, so a stable file in that band keeps its wrong hash
 *  forever, and central cannot repair it (it does not host the file and holds no
 *  hash provenance for agent rows).
 *
 *  Defaults to the defect band (65537..131072); `min_size`/`max_size` widen it
 *  for the opt-in small-file `content_hash` backfill. Resumable, and a repeat at
 *  an unchanged scheme+band short-circuits — `force` re-sweeps anyway.
 *  409 while a sweep is already queued or running for that agent. */
export const rehashSweepAgent = (
  id: string,
  opts: { force?: boolean; max_items?: number; min_size?: number; max_size?: number } = {},
) =>
  request<AgentCommandOut>(`/agents/${id}/rehash-sweep`, {
    method: "POST",
    body: JSON.stringify(opts),
  });

/** HARD delete an agent row — the cleanup path for failed enrollments and
 *  data-free decommissions. 409 while any library/item references the agent. */
/** Hard-delete an agent. `deleteLibraries` also removes every library it
 *  owns (items/scan history cascade; Meili pruned) in the same action —
 *  otherwise the delete is refused (409) while the agent owns data. */
export const deleteAgent = (id: string, deleteLibraries = false) =>
  request<AgentOut>(
    `/agents/${id}?purge=true${deleteLibraries ? "&delete_libraries=true" : ""}`,
    { method: "DELETE" },
  );

// --------------------------------------------------------------------------- //
// P10-T1 — agent_commands (on-demand command primitive). Admin/read surface:   //
// list an agent's commands + cancel a pre-terminal one. Enqueue + the agent    //
// plane (poll/ack/complete) are driven by the agent runtime / retrieve flow.   //
// --------------------------------------------------------------------------- //
export type AgentCommandStatus =
  | "pending"
  | "picked_up"
  | "done"
  | "failed"
  | "expired"
  | "cancelled";
export type AgentCommandKind =
  | "stat_check"
  | "rehash_check"
  | "stage_upload"
  | "inventory"
  | "self_update"
  | "suspend"
  | "agent_maintenance"
  | "reextract"
  // QH-T6. Deliberately NOT a variant of `rehash_check` above: that one is
  // item-scoped and verifies a single file, this one is agent-scoped and
  // migrates a whole size band of the agent's index.
  | "rehash_sweep";

export interface AgentCommandOut {
  id: string;
  agent_id: string;
  kind: AgentCommandKind;
  /** Null for agent-scoped kinds (self_update / suspend / agent_maintenance /
   *  reextract / rehash_sweep). */
  item_id: string | null;
  payload: Record<string, unknown>;
  status: AgentCommandStatus;
  attempts: number;
  created_at: string;
  updated_at: string;
  expires_at: string;
  picked_up_at: string | null;
  completed_at: string | null;
  result: Record<string, unknown> | null;
  requested_by: string | null;
}

export const AGENT_COMMAND_TERMINAL: AgentCommandStatus[] = [
  "done",
  "failed",
  "expired",
  "cancelled",
];

/** Newest-first command listing. `kind` / `state` are the server-side filters
 *  the endpoint already exposes — the fleet console uses them to answer "is a
 *  sweep in flight on this agent?" in ONE request for the whole page instead of
 *  one per row (`update_pending` is the equivalent answer for self_update, but
 *  it is computed per-kind on the agents list). */
export const listAgentCommands = (
  agentId?: string,
  limit = 50,
  filters: { kind?: AgentCommandKind; state?: AgentCommandStatus } = {},
) =>
  request<AgentCommandOut[]>(
    `/agent-commands?${new URLSearchParams({
      ...(agentId ? { agent_id: agentId } : {}),
      ...(filters.kind ? { kind: filters.kind } : {}),
      ...(filters.state ? { state: filters.state } : {}),
      limit: String(limit),
    })}`,
  );

export const cancelAgentCommand = (id: string) =>
  request<AgentCommandOut>(`/agent-commands/${id}/cancel`, { method: "POST" });

// --------------------------------------------------------------------------- //
// P10-T12 — central agent share-maps (admin CRUD, user-mandated). Define how a  //
// path on an agent maps to a network share so an agent-hosted file still gets a  //
// network-open link when the agent can't self-report one (P10-T11). Longest-     //
// local_prefix-wins resolution; an agent-scoped rule outranks a global one.      //
// All behind the agents feature gate; mutations require admin scope.             //
// --------------------------------------------------------------------------- //
export interface ShareLocationOut {
  url: string | null;
  unc: string | null;
}

export interface AgentShareMapOut {
  id: string;
  agent_id: string | null; // null = any agent (global fallback)
  library_id: string | null;
  local_prefix: string;
  share_prefix: string;
  unc: string | null;
  storage_type: string | null;
  host: string | null;
  created_at: string;
  updated_at: string;
  location: ShareLocationOut; // both-format preview of local_prefix itself
}

export interface AgentShareMapCreate {
  library_id?: string | null;
  local_prefix: string;
  share_prefix: string;
  unc?: string | null;
  storage_type?: string | null;
  host?: string | null;
}

export const listAgentShareMaps = (agentId?: string) =>
  request<AgentShareMapOut[]>(
    `/agent-share-maps?${new URLSearchParams(agentId ? { agent_id: agentId } : {})}`,
  );

export const createAgentShareMap = (agentId: string, body: AgentShareMapCreate) =>
  request<AgentShareMapOut>(`/agents/${agentId}/share-maps`, {
    method: "POST",
    body: JSON.stringify(body),
  });

export const updateAgentShareMap = (
  id: string,
  patch: Partial<AgentShareMapCreate>,
) =>
  request<AgentShareMapOut>(`/agent-share-maps/${id}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });

export async function deleteAgentShareMap(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/agent-share-maps/${id}`, {
    method: "DELETE",
    credentials: "same-origin",
    headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
  });
  if (!res.ok && res.status !== 204) throw new ApiError(res.status, await res.text());
}

// --------------------------------------------------------------------------- //
// W6-D4 — agent management page: fleet summary tallies, config-group CRUD +     //
// assignment, and console installer distribution. Types mirror the W6-D2 frozen //
// backend shapes (filearr.agent_config.GroupSettings + the installer contract). //
// --------------------------------------------------------------------------- //

/** GET /agents/summary — status-header counts (read scope). connected +
 *  disconnected = active (cert-bound) agents split by liveness; pending/revoked
 *  are lifecycle buckets; total is the sum of all four. */
export interface AgentFleetSummary {
  total: number;
  connected: number;
  disconnected: number;
  pending: number;
  revoked: number;
}

export const getAgentSummary = () =>
  request<AgentFleetSummary>("/agents/summary");

/** The named folder-selection presets a config group's scan_selections may use
 *  (mirrors filearr.agent_config.SCAN_PRESET_NAMES). "custom" is the empty,
 *  admin-defined scaffold. */
export const SCAN_PRESET_NAMES = [
  "user-documents",
  "user-media",
  "user-profiles-full",
  "downloads",
  "server-data",
  "custom",
] as const;
export type ScanPresetName = (typeof SCAN_PRESET_NAMES)[number];

/** Config-group log levels (mirrors filearr.agent_config.LOG_LEVELS). */
export const AGENT_LOG_LEVELS = ["error", "warn", "info", "verbose", "debug"] as const;
export type AgentLogLevel = (typeof AGENT_LOG_LEVELS)[number];

/** One path selection an agent walks. Either a preset OR explicit path specs
 *  (or both); include/exclude regexes refine matches; enabled gates it. */
export interface ScanSelection {
  preset?: string | null;
  paths?: string[];
  include_regex?: string[];
  exclude_regex?: string[];
  enabled?: boolean;
}

/** Change-audit knobs for the `permissions` collector (W7). Validated + stored
 *  by central ahead of the collector — nothing consumes it yet. */
export interface AuditConfig {
  enabled?: boolean;
  /** Per-path snapshot-history depth, 1..1000. */
  retain_snapshots?: number;
  alert_on_change?: boolean;
  /** Path specs (syntax-validated only), max 200. */
  watch_paths?: string[];
}

/** Detailed config for the `permissions` collector (W7). Only takes effect when
 *  "permissions" is ALSO named in `inventory.collectors` — an admin must both
 *  name the collector and configure it. Agent-side consumption is scaffold. */
export interface PermissionsConfig {
  enabled?: boolean;
  resolve_names?: boolean;
  include_inherited?: boolean;
  /** Reserved for v2 — the agent no-ops on it until shipped. */
  include_effective_access?: boolean;
  exclude_well_known?: boolean;
  /** canonical_id strings, max 64. */
  exclude_principals?: string[];
  collect_share_acls?: boolean;
  audit?: AuditConfig | null;
}

export interface InventoryConfig {
  enabled?: boolean;
  collectors?: string[];
  permissions?: PermissionsConfig | null;
}

/** Field bounds mirrored from filearr.agent_config so the console can validate
 *  before the server 422s. */
export const MAX_RETAIN_SNAPSHOTS = 1000;

/** The typed config-group settings object (v1). Unknown top-level keys are
 *  REJECTED by the backend (422) — keep this in lockstep with GroupSettings. */
export interface GroupSettings {
  log_level?: AgentLogLevel | null;
  scan_selections?: ScanSelection[] | null;
  inventory?: InventoryConfig | null;
  scan_schedule_cron?: string | null;
  /** Per-group local-surface gates. Absent/null = inherit (a lower-priority
   *  group's value, else the agent default). Delivery LIFTS these to the top
   *  level of the document, where a non-null settings value beats the merged
   *  policy key of the same name. */
  web_ui_enabled?: boolean | null;
  local_access_enabled?: boolean | null;
  auth_required?: boolean | null;
}

// --------------------------------------------------------------------------- //
// P13 — configuration groups: ONE grouping, layered by priority                 //
//                                                                               //
// A group carries two sections: `settings` (typed GroupSettings, extra=forbid)  //
// and `policy` (the agent policy document, extra=allow). An agent is in the     //
// permanent Global group plus any number of explicit groups; the effective      //
// document is a per-KEY merge in ascending `priority` (later wins), tie-broken  //
// by (name, id). This replaced whole-document policy scopes, the second        //
// per-agent grouping and the staged-release cohort — none of those concepts     //
// exist any more, on either end.                                                //
// --------------------------------------------------------------------------- //

/** One phase of a rollout: `percent` of the fleet (by stable agent-id hash
 *  bucket) covered once this tier activates, `delay_minutes` after the PREVIOUS
 *  tier activated (tier 0 counts from the rollout's start). */
export interface RolloutTier {
  percent: number;
  delay_minutes: number;
}

/** The live rollout embedded in a group row — enough to render a status chip
 *  without a second request. `current_tier` is -1 until the first tier fires. */
export interface ActiveRolloutOut {
  id: string;
  status: "scheduled" | "running";
  current_tier: number;
  tiers: RolloutTier[];
  target_version: number;
  starts_at: string | null;
  started_at: string | null;
  tier_started_at: string | null;
}

export interface ConfigGroupOut {
  id: string;
  name: string;
  description: string | null;
  settings: GroupSettings;
  /** The policy half of the group document (extra="allow" — unmodelled keys
   *  round-trip verbatim; see ./agentPolicyDoc). */
  policy: AgentPolicyDoc;
  /** Merge rank: LOWER applies first, so a HIGHER number wins a contested key.
   *  Global is pinned at 0 and immutable. */
  priority: number;
  /** True only for the permanent Global group: undeletable, un-renamable,
   *  priority-locked, and implicitly containing every agent. */
  is_system: boolean;
  current_version: number;
  /** Explicit members — except for Global, where it is the WHOLE fleet count. */
  member_count: number;
  active_rollout: ActiveRolloutOut | null;
  created_at: string;
  updated_at: string;
}

/** One published snapshot of a group document. `seq` is the fleet-wide
 *  generation counter (what an agent echoes back); `version` is the per-group
 *  number an operator reads and rolls back to. */
export interface ConfigVersionOut {
  seq: number;
  version: number;
  settings: GroupSettings;
  policy: AgentPolicyDoc;
  actor: string | null;
  note: string | null;
  created_at: string;
}

export interface ConfigGroupDetailOut extends ConfigGroupOut {
  /** Newest-first, capped at 20 by the server — page the rest via history(). */
  versions: ConfigVersionOut[];
}

export interface ConfigGroupIn {
  name: string;
  description?: string | null;
  settings?: GroupSettings;
  policy?: AgentPolicyDoc;
  priority?: number;
}

/** PATCH body. `settings` and `policy` REPLACE their section wholesale —
 *  authoring is replacement, layering happens ACROSS groups. Passing `rollout`
 *  publishes the new version behind a phased rollout instead of immediately:
 *  `current_version` stays put and uncovered agents keep receiving it. */
export interface ConfigGroupUpdateIn {
  name?: string;
  description?: string | null;
  priority?: number;
  settings?: GroupSettings;
  policy?: AgentPolicyDoc;
  note?: string;
  rollout?: { tiers: RolloutTier[]; starts_at?: string | null };
}

/** Ordered by (priority, name) — i.e. in MERGE order, so the table reads
 *  top-to-bottom as "what overrides what". Global is always first. */
export const listConfigGroups = () =>
  request<ConfigGroupOut[]>("/agents/config-groups");

export const getConfigGroup = (id: string) =>
  request<ConfigGroupDetailOut>(`/agents/config-groups/${id}`);

/** Version history, newest-first. `before` is the last `version` of the
 *  previous page (keyset, strict <). */
export const listConfigGroupHistory = (id: string, limit = 20, before?: number) =>
  request<ConfigVersionOut[]>(
    `/agents/config-groups/${id}/history?limit=${limit}` +
      (before !== undefined ? `&before=${before}` : ""),
  );

/** Copy an old snapshot forward as a NEW version and publish it IMMEDIATELY —
 *  a rollback also cancels any live rollout. Versioning stays forward-only, so
 *  nothing is ever rewritten in place. */
export const rollbackConfigGroup = (id: string, version: number, note?: string) =>
  request<ConfigGroupOut>(`/agents/config-groups/${id}/rollback`, {
    method: "POST",
    body: JSON.stringify({ version, ...(note ? { note } : {}) }),
  });

/** A phased rollout of one group version. `covered_percent` is the fleet share
 *  already receiving `target_version`; `next_promotion_at` is null unless it is
 *  running with a tier still to come. */
export interface RolloutOut {
  id: string;
  group_id: string;
  group_name: string;
  target_version: number;
  tiers: RolloutTier[];
  status: "scheduled" | "running" | "completed" | "cancelled";
  current_tier: number;
  covered_percent: number;
  next_promotion_at: string | null;
  starts_at: string | null;
  started_at: string | null;
  tier_started_at: string | null;
  finished_at: string | null;
  actor: string | null;
  created_at: string;
}

/** Omitting `status` returns only the LIVE ones (scheduled + running) — which
 *  is all the console's rollouts card ever wants. */
export const listConfigRollouts = (status?: string, limit = 50) =>
  request<RolloutOut[]>(
    `/agents/config-rollouts?limit=${limit}` +
      (status ? `&status=${encodeURIComponent(status)}` : ""),
  );

/** Cancel: the rollout stops and `current_version` is left alone, so agents
 *  already covered by a tier FALL BACK to it on their next poll. */
export const cancelConfigRollout = (id: string) =>
  request<RolloutOut>(`/agents/config-rollouts/${id}/cancel`, { method: "POST" });

/** Advance to the next tier now, ignoring the remaining delay. 409 unless the
 *  rollout is actually running. */
export const promoteConfigRollout = (id: string) =>
  request<RolloutOut>(`/agents/config-rollouts/${id}/promote`, { method: "POST" });

export interface MembershipOut {
  agent_id: string;
  group_ids: string[];
  groups: { id: string; name: string; priority: number }[];
}

/** FULL REPLACE of an agent's explicit memberships (`[]` removes them all).
 *  Global is implicit — including its id is a 400, not a no-op. */
export const setAgentConfigGroups = (agentId: string, group_ids: string[]) =>
  request<MembershipOut>(`/agents/${agentId}/config-groups`, {
    method: "PUT",
    body: JSON.stringify({ group_ids }),
  });

/** One contributing layer of an agent's effective document, in merge order.
 *  `via_rollout` marks a group whose version came from a rollout tier this
 *  agent's hash bucket is covered by, rather than from `current_version`. */
export interface EffectiveGroupRef {
  id: string;
  name: string;
  priority: number;
  is_system: boolean;
  version_used: number;
  via_rollout: boolean;
}

/** Which group version supplied one merged key. Keys of `provenance` are
 *  `"policy.<key>"` / `"settings.<key>"`. */
export interface ProvenanceEntry {
  group_id: string;
  group_name: string;
  version: number;
}

/** GET /agents/{id}/effective-config (admin) — the merged document this agent
 *  receives on its next poll, with per-key provenance.
 *
 *  `document` is the wire shape: merged POLICY keys at the top level, the merged
 *  SETTINGS section under `group`, and the three lifted local-surface keys.
 *  `taxonomy_version` is deliberately absent — central injects it per agent-plane
 *  response and it is never operator-set. */
export interface EffectiveConfigOut {
  agent_id: string;
  document: Record<string, unknown>;
  /** max(seq) over the contributing snapshots — the number an agent echoes. */
  generation: number;
  /** First 12 hex of sha256 over the canonical document. */
  hash: string;
  groups: EffectiveGroupRef[];
  provenance: Record<string, ProvenanceEntry>;
  /** What the agent last confirmed. Behind `generation` = not applied yet. */
  confirmed_generation: number | null;
  last_seen_at: string | null;
}

export const getEffectiveConfig = (agentId: string) =>
  request<EffectiveConfigOut>(
    `/agents/${encodeURIComponent(agentId)}/effective-config`,
  );

/** The inventory-collector vocabulary for the config-group dialog's checkbox
 *  list: the UNION of the shipped catalogue and every collector name the
 *  enrolled fleet advertises. A UI catalogue, NEVER a validation whitelist —
 *  `inventory.collectors` stays free-form so a newer agent's collector works
 *  without a central release. Admin scope; fetched when the dialog opens (not
 *  on page load) because it costs a query over the agents table.
 *  Shapes + merge rules live in ./inventoryCollectors. */
export const listInventoryCollectors = () =>
  request<CollectorCatalogueEntry[]>("/agents/inventory-collectors");

/** The published minimum version of each extraction host tool, with the
 *  one-sentence consequence of being below it and the justification for the
 *  number. Static, fleet-wide data — fetched once per Agents page load and used
 *  only to write the tooltip behind a chip whose verdict central already
 *  computed, so a failed fetch degrades the explanation and never the warning.
 *  Shapes + chip rules live in ./hostTools. */
export const listHostToolMinimums = () =>
  request<HostToolMinimum[]>("/agents/host-tool-minimums");

/** GET /agents/{id}/about (admin) — the per-agent About / dependency report:
 *  build stack, Go module dependencies, and host tools with version, resolved
 *  PATH and central's verdict against the published minimums.
 *
 *  The per-agent counterpart of `GET /system/about`, with one crucial
 *  difference in kind: that endpoint probes the machine it is running on, this
 *  one reads what the agent last SELF-REPORTED on its command poll. Nothing is
 *  queried from the agent — central never calls out to agents, agents poll — so
 *  every value is as of `agent.capabilities_at`.
 *
 *  `admin` scope rather than `read` (which `/system/about` uses), deliberately:
 *  this exposes filesystem paths from someone else's machine.
 *
 *  Shapes, cell rules and the Markdown dump live in ./agentAbout; status chips
 *  come from ./hostTools so there is no second verdict vocabulary. */
export const agentAbout = (agentId: string) =>
  request<AgentAbout>(`/agents/${encodeURIComponent(agentId)}/about`);

export const createConfigGroup = (body: ConfigGroupIn) =>
  request<ConfigGroupOut>("/agents/config-groups", {
    method: "POST",
    body: JSON.stringify(body),
  });

export const updateConfigGroup = (id: string, patch: ConfigGroupUpdateIn) =>
  request<ConfigGroupOut>(`/agents/config-groups/${id}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });

export async function deleteConfigGroup(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/agents/config-groups/${id}`, {
    method: "DELETE",
    credentials: "same-origin",
    headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
  });
  if (!res.ok && res.status !== 204) throw new ApiError(res.status, await res.text());
}

// ---- console installer distribution (POST /agents/installer-config) ----------
export interface InstallerConfigIn {
  central_url_override?: string | null;
  agent_name?: string | null;
  /** Groups to join at enrollment, by id. Global is implicit and is silently
   *  skipped if passed. */
  config_group_ids?: string[];
  log_level?: string | null;
  ttl_seconds?: number | null;
}

export interface InstallerSidecar {
  central_url: string;
  enrollment_token: string; // raw, show-once
  agent_name: string | null;
  /** P13: every group the agent joins, by NAME. */
  config_group_names: string[];
  /** LEGACY single-group key (= config_group_names[0]), still emitted so a
   *  shipped agent binary that only reads this one keeps working. */
  config_group: string | null;
  log_level: string | null;
}

export interface InstallHint {
  windows: string;
  linux: string;
  macos: string;
}

export interface InstallerConfigOut {
  sidecar: InstallerSidecar;
  token_hash: string;
  expires_at: string;
  install_hint: InstallHint;
}

export const issueInstallerConfig = (body: InstallerConfigIn) =>
  request<InstallerConfigOut>("/agents/installer-config", {
    method: "POST",
    body: JSON.stringify(body),
  });

// --------------------------------------------------------------------------- //
// LLM keys (M1, docs/research/llm-rag-integration.md §5)                       //
// --------------------------------------------------------------------------- //

export interface LlmRoleInfo {
  name: string;
  description: string;
  tools: string[];
  content_access: boolean;
  reveal_paths: boolean;
}

export interface LlmKey {
  id: string;
  name: string;
  prefix: string;
  role: string;
  role_description: string | null;
  path_scope: string | null;
  libraries: string[] | null;
  content_access: boolean;
  reveal_paths: boolean;
  rate_limit: number;
  expires_at: string | null;
  last_used_at: string | null;
  created_at: string | null;
  /** M3 usage dashboard: audited tool calls (list endpoint only). */
  tool_calls?: number;
  last_call_at?: string | null;
  /** present ONLY in the mint response — shown once. */
  key?: string;
}

export interface MintLlmKeyRequest {
  name: string;
  role: string;
  path_scope?: string | null;
  libraries?: string[] | null;
  content_access?: boolean | null;
  reveal_paths?: boolean | null;
  rate_limit?: number | null;
  expires_days?: number | null;
}

export function listLlmRoles(): Promise<{ roles: LlmRoleInfo[] }> {
  return request("/llm-keys/roles");
}

export function listLlmKeys(): Promise<{ keys: LlmKey[] }> {
  return request("/llm-keys");
}

export function mintLlmKey(body: MintLlmKeyRequest): Promise<LlmKey> {
  return request("/llm-keys", { method: "POST", body: JSON.stringify(body) });
}

export async function revokeLlmKey(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/llm-keys/${id}`, {
    method: "DELETE",
    headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
  });
  if (!res.ok) throw new ApiError(res.status, await res.text());
}

// --- Ordinary API keys (read / write / admin Bearer tokens; admin-minted) ---
export type ApiKeyScope = "read" | "write" | "admin";

export interface ApiKeyRow {
  id: string;
  name: string;
  prefix: string;
  scopes: ApiKeyScope[];
  expires_at: string | null;
  expired: boolean;
  last_used_at: string | null;
  created_at: string | null;
  /** present ONLY in the mint response — shown once. */
  key?: string;
}

export interface ApiKeyScopeInfo {
  name: ApiKeyScope;
  description: string;
}

export function listApiKeyScopes(): Promise<{ scopes: ApiKeyScopeInfo[] }> {
  return request("/api-keys/scopes");
}

export function listApiKeys(): Promise<{ keys: ApiKeyRow[] }> {
  return request("/api-keys");
}

export function mintApiKey(body: {
  name: string;
  scopes: ApiKeyScope[];
  expires_days?: number | null;
}): Promise<ApiKeyRow> {
  return request("/api-keys", { method: "POST", body: JSON.stringify(body) });
}

export async function revokeApiKey(id: string): Promise<void> {
  const res = await fetch(`${API_BASE}/api-keys/${id}`, {
    method: "DELETE",
    headers: { ...(KEY() ? { Authorization: `Bearer ${KEY()}` } : {}) },
  });
  if (!res.ok) throw new ApiError(res.status, await res.text());
}
