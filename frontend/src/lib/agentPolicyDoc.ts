// Agent policy document <-> form model (2026-08-09).
//
// The DOM-free core behind AgentPolicyEditor.svelte, kept here so the two rules
// that would be DATA LOSS bugs if they regressed are unit-testable on Node:
//
//  1. **Unknown-key preservation.** The backend's PolicyModel is `extra="allow"`
//     and stores the submitted document VERBATIM. A newer agent build may read a
//     key this console has never heard of. The form must therefore round-trip
//     every key it does not render (`passthroughFromDoc` -> `buildPolicyDoc`)
//     instead of rebuilding the document from its own field list.
//  2. **"Inherit" is an ABSENT key, not a null.** Policy resolution is
//     most-specific-wins with NO key merging, and an absent key means "agent
//     default". Emitting `{"watch_mode": null}` would be a different (and for
//     several keys, invalid) document than omitting it.
//
// Every field is therefore tri-state: `{ set: false }` = inherit/absent,
// `{ set: true, value }` = an explicit value in this scope's document.

/** A policy document as stored. All known keys optional; the index signature
 *  carries forward-compat keys central does not model (preserved verbatim). */
export interface AgentPolicyDoc {
  presets?: string[];
  include_globs?: string[];
  exclude_globs?: string[];
  content_hash_max_bytes?: number;
  watch_mode?: boolean;
  reconcile_interval_seconds?: number;
  poll_interval_seconds?: number;
  local_access_enabled?: boolean;
  web_ui_enabled?: boolean;
  auth_required?: boolean;
  read_only?: boolean;
  path_scope?: string[];
  offline_grace_seconds?: number;
  scan_cron?: string;
  scan_interval_seconds?: number;
  scan_on_start?: boolean;
  upload_rate_bytes_per_sec?: number;
  auto_update?: boolean;
  extract_enabled?: boolean;
  extract_body_text?: boolean;
  extract_ocr?: boolean;
  extract_exif?: boolean;
  extract_max_bytes?: number;
  local_scan_control?: boolean;
  local_schedule_control?: boolean;
  local_roots_control?: boolean;
  [key: string]: unknown;
}

export type PolicySection =
  | "scanning"
  | "extraction"
  | "scheduling"
  | "polling"
  | "local"
  | "updates";

export const POLICY_SECTIONS: { id: PolicySection; label: string; blurb: string }[] = [
  {
    id: "scanning",
    label: "Scanning",
    blurb: "What the agent walks and how hard it hashes.",
  },
  {
    id: "extraction",
    label: "Content extraction",
    blurb:
      "What the agent extracts from the files it catalogs, and ships up with its " +
      "change events. Central never opens a file on an agent host, so this is the " +
      "only way agent items get real content metadata. Capability is a property of " +
      "the AGENT HOST (ffprobe / exiftool / poppler-utils / tesseract on PATH), not " +
      "of the build — the agents table flags any setting a given agent cannot honour.",
  },
  {
    id: "scheduling",
    label: "Scheduling",
    blurb:
      "The in-daemon scan scheduler. All three absent = scheduler off (container " +
      "deployments keep their entrypoint loop instead — don't enable both).",
  },
  {
    id: "polling",
    label: "Polling & sync",
    blurb: "How often the agent talks to central, and how fast it may upload.",
  },
  {
    id: "local",
    label: "Local access",
    blurb:
      "The agent's on-device query surfaces (CLI socket + read-only web UI), what " +
      "they are allowed to return, and how much of the agent's OWN administration " +
      "(pause, schedule, scan roots) its local operator may change. The catalog " +
      "stays read-only locally whatever these say.",
  },
  { id: "updates", label: "Updates", blurb: "Self-update offers." },
];

export type PolicyFieldKind = "bool" | "int" | "lines" | "cron" | "presets";

/** Who actually acts on a key TODAY — surfaced honestly in the UI so an
 *  operator never believes a stored setting is doing something it isn't. */
export type EnforcedBy = "agent" | "central";

export interface PolicyFieldSpec {
  key: string;
  label: string;
  kind: PolicyFieldKind;
  section: PolicySection;
  /** Plain text (rendered as text, never {@html}). */
  hint: string;
  min?: number;
  max?: number;
  /** What happens when the key is absent from the winning document. */
  fallback: string;
  enforcedBy: EnforcedBy;
}

export const POLICY_FIELDS: PolicyFieldSpec[] = [
  // --- Scanning ------------------------------------------------------------
  {
    key: "presets",
    label: "Exclusion presets",
    kind: "presets",
    section: "scanning",
    hint: "Named exclusion bundles the agent applies while walking. Validated against central's preset catalogue on save.",
    fallback: "the agent's built-in preset defaults",
    enforcedBy: "agent",
  },
  {
    key: "include_globs",
    label: "Include globs",
    kind: "lines",
    section: "scanning",
    hint: "One glob per line. Only matching paths are cataloged. An explicit empty list means 'include nothing extra' — leave the field on Inherit to say nothing at all.",
    fallback: "no include filter (everything not excluded)",
    enforcedBy: "agent",
  },
  {
    key: "exclude_globs",
    label: "Exclude globs",
    kind: "lines",
    section: "scanning",
    hint: "One glob per line, applied on top of the preset bundles.",
    fallback: "presets only",
    enforcedBy: "agent",
  },
  {
    key: "content_hash_max_bytes",
    label: "Content-hash size cap (bytes)",
    kind: "int",
    section: "scanning",
    min: 0,
    hint: "Files larger than this are cataloged without a content hash. 0 disables content hashing entirely.",
    fallback: "the agent's built-in cap",
    enforcedBy: "agent",
  },
  {
    key: "watch_mode",
    label: "Watch mode",
    kind: "bool",
    section: "scanning",
    hint: "Filesystem-event watching instead of pure polling. Local disks only — inotify is unreliable over SMB/NFS.",
    fallback: "off (polling)",
    enforcedBy: "agent",
  },
  // --- Content extraction --------------------------------------------------
  {
    key: "extract_enabled",
    label: "Agent-side extraction",
    kind: "bool",
    section: "extraction",
    hint: "Run the extraction pass on the agent and ship the result with each change event. Off means agent items carry identity only (path/size/mtime/hashes) — the other keys in this section then do nothing.",
    fallback: "off — identity-only replication",
    enforcedBy: "agent",
  },
  {
    key: "extract_body_text",
    label: "Include document body text",
    kind: "bool",
    section: "extraction",
    hint: "Extract text from documents (txt/md/docx/xlsx/odf/epub…, and PDF where pdftotext is installed on the agent host). This is what makes agent items chunkable and content-embeddable instead of filename-only — and what makes replication events materially larger. Oversize payloads are dropped by central, not retried.",
    fallback: "off — metadata only, no body text",
    enforcedBy: "agent",
  },
  {
    key: "extract_ocr",
    label: "OCR images and scanned PDFs",
    kind: "bool",
    section: "extraction",
    hint: "Needs tesseract installed on the AGENT host, plus pdftoppm (poppler-utils) for the scanned-PDF half. A PDF that already has a usable text layer is never OCR'd. An agent missing a tool logs the ignored setting and carries on; the agents table shows which agents those are.",
    fallback: "off",
    enforcedBy: "agent",
  },
  {
    key: "extract_exif",
    label: "Deep EXIF for images",
    kind: "bool",
    section: "extraction",
    hint: "Camera, lens, exposure, focal length and GPS, read with exiftool on the AGENT host. Off by default even though central does this automatically: on an agent it costs one subprocess per image inside the scan, and it sends GPS coordinates to central (where they stay hidden unless the library sets expose_gps). Image dimensions and format do not need it.",
    fallback: "off — dimensions and format only",
    enforcedBy: "agent",
  },
  {
    key: "extract_max_bytes",
    label: "Extraction size cap (bytes)",
    kind: "int",
    section: "extraction",
    min: 0,
    hint: "Files larger than this are cataloged but not extracted (the identity half of the event is unaffected). 0 = extract nothing.",
    fallback: "the agent's built-in cap (32 MiB)",
    enforcedBy: "agent",
  },
  // --- Scheduling ----------------------------------------------------------
  {
    key: "scan_cron",
    label: "Scan schedule (cron)",
    kind: "cron",
    section: "scheduling",
    hint: "5-field cron in the AGENT's local time (not UTC, and not this browser's timezone). Wins over the interval below when both are set.",
    fallback: "no cron schedule",
    enforcedBy: "agent",
  },
  {
    key: "scan_interval_seconds",
    label: "Scan interval (seconds)",
    kind: "int",
    section: "scheduling",
    min: 300,
    hint: "Fixed-interval scanning. Minimum 300s. Ignored when a scan cron is set.",
    fallback: "no interval schedule",
    enforcedBy: "agent",
  },
  {
    key: "scan_on_start",
    label: "Scan on daemon start",
    kind: "bool",
    section: "scheduling",
    hint: "Fire one scan roughly 30 seconds after the agent daemon starts.",
    fallback: "off",
    enforcedBy: "agent",
  },
  // --- Polling & sync ------------------------------------------------------
  {
    key: "poll_interval_seconds",
    label: "Policy poll interval (seconds)",
    kind: "int",
    section: "polling",
    min: 60,
    max: 86400,
    hint: "How often the agent polls central for policy/commands. 60–86400s. Longer intervals delay every setting on this page.",
    fallback: "the agent's built-in poll interval",
    enforcedBy: "agent",
  },
  {
    key: "reconcile_interval_seconds",
    label: "Reconcile interval (seconds)",
    kind: "int",
    section: "polling",
    min: 300,
    hint: "How often the agent pages its whole manifest to central for a full-manifest diff (the safety net behind incremental replication). Minimum 300s.",
    fallback: "24 hours",
    enforcedBy: "agent",
  },
  {
    key: "upload_rate_bytes_per_sec",
    label: "Upload rate cap (bytes/sec)",
    kind: "int",
    section: "polling",
    min: 0,
    hint: "Token-bucket ceiling for staged uploads. 0 = unlimited. Read at upload START — a change applies to the next upload, not one in flight.",
    fallback: "unlimited",
    enforcedBy: "agent",
  },
  // --- Local access --------------------------------------------------------
  {
    key: "local_access_enabled",
    label: "Local query API / CLI",
    kind: "bool",
    section: "local",
    hint: "The on-device 'filearr query' socket. An explicit off persists through offline periods (the policy is cached).",
    fallback: "on",
    enforcedBy: "agent",
  },
  {
    key: "web_ui_enabled",
    label: "Local web UI",
    kind: "bool",
    section: "local",
    hint: "Read-only browser search on the agent (loopback 127.0.0.1:8686). Fails closed when the cached policy goes stale.",
    fallback: "off — a never-contacted agent serves nothing",
    enforcedBy: "agent",
  },
  {
    key: "auth_required",
    label: "Web UI requires auth token",
    kind: "bool",
    section: "local",
    hint: "The local web UI demands the agent's bootstrap token before serving. Never affects the CLI peer-credential check.",
    fallback: "on",
    enforcedBy: "agent",
  },
  {
    key: "offline_grace_seconds",
    label: "Offline grace (seconds)",
    kind: "int",
    section: "local",
    min: 0,
    hint: "How long a cached policy stays trusted while the agent is disconnected. Past it the local web UI fails closed; the CLI keeps answering.",
    fallback: "86400 (24 hours)",
    enforcedBy: "agent",
  },
  {
    key: "path_scope",
    label: "Path scope (rel_path globs)",
    kind: "lines",
    section: "local",
    hint: "One glob per line, OR-combined, applied to every LOCAL result set. Empty/absent = unrestricted. Max 1000 entries.",
    fallback: "unrestricted",
    enforcedBy: "agent",
  },
  // --- Local self-administration (2026-08-10) -------------------------------
  // These three delegate a slice of AGENT administration to whoever is at the
  // machine. They are a different axis from read_only, which still holds: the
  // local surface never writes to the catalog whatever these say.
  {
    key: "local_scan_control",
    label: "Local pause / resume / scan now",
    kind: "bool",
    section: "local",
    hint: "Lets the agent's own web UI pause and resume ITS scanning and trigger a scan. Agent self-administration only — never catalog edits; the local surface stays read-only over items and metadata. The local pause is separate from the Suspend action here: a local resume cannot lift a central suspend.",
    fallback: "off — scanning is controlled only from this console",
    enforcedBy: "agent",
  },
  {
    key: "local_schedule_control",
    label: "Local schedule editing",
    kind: "bool",
    section: "local",
    hint: "Lets the agent's own web UI set the scan cron, interval and scan-on-start — but only the ones this policy leaves unset. A key you set here is locked on the agent and shown as 'managed by central', because central re-applies it every poll and a local edit would silently revert. Agent self-administration only; never catalog edits.",
    fallback: "off — the schedule comes only from policy and the host's environment",
    enforcedBy: "agent",
  },
  {
    key: "local_roots_control",
    label: "Local scan-root editing",
    kind: "bool",
    section: "local",
    hint: "Lets the agent's own web UI add and remove ITS scan roots (its local scan.json). Refused anyway when the agent's config group derives roots from scan_selections. Removing a root only stops future scans of it — already-indexed items are left alone. Agent self-administration only; never catalog edits.",
    fallback: "off — roots come from the agent's own config or its config group",
    enforcedBy: "agent",
  },
  // --- Updates -------------------------------------------------------------
  {
    key: "auto_update",
    label: "Offer self-updates",
    kind: "bool",
    section: "updates",
    hint: "Whether central OFFERS an update on this agent's update-manifest poll. Enforced by CENTRAL (the poll answers 204 when off), so it applies to every agent build. An operator-triggered update from the agents table bypasses it.",
    fallback: "on",
    enforcedBy: "central",
  },
];

/** Keys the form renders as editable fields. */
export const EDITABLE_POLICY_KEYS: readonly string[] = POLICY_FIELDS.map((f) => f.key);

/** Known keys the form deliberately does NOT render, with why. They are still
 *  preserved verbatim on save and must not be reported as "unknown". */
export const RESERVED_POLICY_KEYS: Record<string, string> = {
  read_only:
    "Always true — the agent's local surface is read-only by invariant. Central rejects a false with a 422.",
  taxonomy_version:
    "Injected by central per response (the file-extension taxonomy revision). Never operator-set.",
  group:
    "Reserved for the assigned config group's settings. An operator-authored 'group' key SUPPRESSES the config-group fold entirely.",
};

export interface PolicyFieldState {
  /** false = inherit (the key is absent from this scope's document). */
  set: boolean;
  /** Stringified editor value. bool -> "true"/"false"; lines -> newline-joined;
   *  presets -> comma/newline-joined; int/cron -> the raw input text. */
  value: string;
}

export type PolicyFormState = Record<string, PolicyFieldState>;

const splitList = (t: string): string[] =>
  t
    .split(/[\n,]/)
    .map((x) => x.trim())
    .filter(Boolean);

const splitLines = (t: string): string[] =>
  t
    .split("\n")
    .map((x) => x.trim())
    .filter(Boolean);

/** An all-inherit form (a scope with no document, or "start over"). */
export function blankPolicyForm(): PolicyFormState {
  const form: PolicyFormState = {};
  for (const f of POLICY_FIELDS) form[f.key] = { set: false, value: "" };
  return form;
}

/** Seed the form from a stored document. A key present with a value of the
 *  WRONG shape (hand-written JSON, or a key that changed type across versions)
 *  is left on "inherit" so the form never silently rewrites it — it stays in the
 *  passthrough set and round-trips untouched. */
export function formFromDoc(doc: AgentPolicyDoc | null | undefined): PolicyFormState {
  const form = blankPolicyForm();
  if (!doc) return form;
  for (const f of POLICY_FIELDS) {
    if (!(f.key in doc)) continue;
    const raw = doc[f.key];
    if (raw === null || raw === undefined) continue;
    switch (f.kind) {
      case "bool":
        if (typeof raw === "boolean") form[f.key] = { set: true, value: String(raw) };
        break;
      case "int":
        if (typeof raw === "number" && Number.isFinite(raw))
          form[f.key] = { set: true, value: String(raw) };
        break;
      case "cron":
        if (typeof raw === "string") form[f.key] = { set: true, value: raw };
        break;
      case "lines":
      case "presets":
        if (Array.isArray(raw) && raw.every((x) => typeof x === "string"))
          form[f.key] = {
            set: true,
            value: (raw as string[]).join(f.kind === "presets" ? ", " : "\n"),
          };
        break;
    }
  }
  return form;
}

/** Every key of `doc` the FORM does not own — reserved keys, forward-compat keys,
 *  and any key whose stored shape the form declined to parse. These are re-emitted
 *  verbatim by `buildPolicyDoc`; dropping one would silently discard operator (or
 *  newer-agent) configuration. */
export function passthroughFromDoc(
  doc: AgentPolicyDoc | null | undefined,
  form?: PolicyFormState,
): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  if (!doc) return out;
  for (const [key, value] of Object.entries(doc)) {
    if (EDITABLE_POLICY_KEYS.includes(key) && (form ? form[key]?.set : true)) continue;
    out[key] = value;
  }
  return out;
}

/** Known field keys whose STORED value the form declined to parse (wrong JSON
 *  shape). They show as "Inherit" but are preserved verbatim, so the editor warns
 *  and points at the raw-JSON view rather than pretending the key is unset. */
export function unparsedPolicyKeys(
  doc: AgentPolicyDoc | null | undefined,
  form: PolicyFormState,
): string[] {
  if (!doc) return [];
  return EDITABLE_POLICY_KEYS.filter(
    (k) => k in doc && doc[k] !== null && doc[k] !== undefined && !form[k]?.set,
  );
}

/** Keys in `doc` this console models neither as a field nor as a reserved key —
 *  i.e. genuinely unknown/forward-compat. Surfaced in the editor so the operator
 *  can see what is riding along untouched. */
export function unknownPolicyKeys(doc: AgentPolicyDoc | null | undefined): string[] {
  if (!doc) return [];
  return Object.keys(doc)
    .filter((k) => !EDITABLE_POLICY_KEYS.includes(k) && !(k in RESERVED_POLICY_KEYS))
    .sort();
}

/** Serialize the form back into a document.
 *
 * `passthrough` (from `passthroughFromDoc`, which is what excludes the keys the
 * form owns) is laid down FIRST so unknown and reserved keys survive;
 * explicitly-set fields then win. Fields on "inherit" are OMITTED — never
 * emitted as null. A field the form could not parse (see `unparsedPolicyKeys`)
 * IS in the passthrough set and therefore survives untouched: dropping it
 * because the form couldn't render it would be exactly the data loss this module
 * exists to prevent. */
export function buildPolicyDoc(
  form: PolicyFormState,
  passthrough: Record<string, unknown> = {},
): AgentPolicyDoc {
  const doc: AgentPolicyDoc = { ...passthrough };
  for (const f of POLICY_FIELDS) {
    const state = form[f.key];
    if (!state?.set) continue;
    switch (f.kind) {
      case "bool":
        doc[f.key] = state.value === "true";
        break;
      case "int":
        doc[f.key] = Number(state.value);
        break;
      case "cron":
        doc[f.key] = state.value.trim();
        break;
      case "lines":
        doc[f.key] = splitLines(state.value);
        break;
      case "presets":
        doc[f.key] = splitList(state.value);
        break;
    }
  }
  return doc;
}

// --------------------------------------------------------------------------- //
// Client-side validation (mirrors the server bounds so a 422 is rare)           //
// --------------------------------------------------------------------------- //

/** Lenient 5-field cron shape check. cronsim on the server is the authority —
 *  this only catches the obvious wrong-field-count / stray-character typo. */
export function cronShapeError(expr: string): string | null {
  const trimmed = expr.trim();
  if (!trimmed) return "cron expression is required";
  const fields = trimmed.split(/\s+/);
  if (fields.length !== 5)
    return `expected 5 fields (minute hour day month weekday), got ${fields.length}`;
  for (const f of fields) {
    if (!/^[0-9A-Za-z*/,\-?]+$/.test(f)) return `unexpected characters in "${f}"`;
  }
  return null;
}

export const MAX_PATH_SCOPE_PREDICATES = 1000;

/** Per-key error messages for the fields currently set. `knownPresets` (from
 *  GET /presets) enables the preset-name check; omit it to skip that check. */
export function validatePolicyForm(
  form: PolicyFormState,
  opts: { knownPresets?: readonly string[] } = {},
): Record<string, string> {
  const errors: Record<string, string> = {};
  for (const f of POLICY_FIELDS) {
    const state = form[f.key];
    if (!state?.set) continue;
    if (f.kind === "int") {
      const raw = state.value.trim();
      if (!raw) {
        errors[f.key] = "a value is required (or switch the field to Inherit)";
        continue;
      }
      const n = Number(raw);
      if (!Number.isFinite(n) || !Number.isInteger(n)) {
        errors[f.key] = "must be a whole number";
        continue;
      }
      if (f.min !== undefined && n < f.min) errors[f.key] = `must be ≥ ${f.min}`;
      else if (f.max !== undefined && n > f.max) errors[f.key] = `must be ≤ ${f.max}`;
    } else if (f.kind === "cron") {
      const err = cronShapeError(state.value);
      if (err) errors[f.key] = err;
    } else if (f.kind === "presets") {
      const known = opts.knownPresets;
      if (known && known.length) {
        const bad = splitList(state.value).filter((p) => !known.includes(p));
        if (bad.length) errors[f.key] = `unknown preset(s): ${bad.join(", ")}`;
      }
    } else if (f.kind === "lines" && f.key === "path_scope") {
      const n = splitLines(state.value).length;
      if (n > MAX_PATH_SCOPE_PREDICATES)
        errors[f.key] = `${n} predicates; max ${MAX_PATH_SCOPE_PREDICATES}`;
    }
  }
  return errors;
}

// --------------------------------------------------------------------------- //
// Capability advertisement vs. effective policy ("what will this agent ignore") //
// --------------------------------------------------------------------------- //

/** The agent's self-reported `capabilities` object (stored verbatim on the agent
 *  row from its command poll). Everything is optional: an older build advertises
 *  fewer keys, and a never-polled agent has no object at all. */
export interface AgentCapabilities {
  /** This build has the extraction pass at all. */
  extract?: boolean;
  /** Extraction vocabulary version the agent produces. */
  extract_schema?: number;
  /** Host tools found on PATH — capability is a HOST property, not a build one. */
  tools?: Record<string, boolean>;
  /** Versions of the tools that are present AND willing to state one. A tool in
   *  `tools` but absent here is installed with an unreportable version, which is
   *  a different thing from not installed — render them differently. */
  tool_versions?: Record<string, string>;
  /** What it can actually extract here, e.g. ["image","audio","document"]. */
  formats?: string[];
  /** Pre-existing key: the agent runs in a container (self-update N/A). */
  container?: boolean;
  [key: string]: unknown;
}

/** The host tools the console renders as an on/off matrix, in display order. */
export const CAPABILITY_TOOLS: readonly string[] = [
  "ffmpeg",
  "ffprobe",
  "tesseract",
  "exiftool",
  // poppler-utils, detected per binary: a host can ship a partial install, and
  // the three back different capabilities (PDF properties / PDF text / scanned-
  // PDF rasterisation for OCR).
  "pdfinfo",
  "pdftotext",
  "pdftoppm",
];

export interface IgnoredSetting {
  key: string;
  /** Plain text (rendered as text, never {@html}). */
  reason: string;
}

/** Effective policy keys THIS agent cannot honour, with why.
 *
 * The point of the capability advertisement: an operator sets `extract_ocr` at
 * the global scope and has no way to know that three of their hosts have no
 * tesseract. Rather than let the setting look applied everywhere, the console
 * cross-references the agent's advertised capabilities against the effective
 * document and names each dead setting.
 *
 * Deliberately CONSERVATIVE — silence beats a false alarm:
 * - no capabilities object at all (never polled, or a pre-advertisement build)
 *   returns `[]`; we know nothing, so we claim nothing;
 * - only settings that would actually DO something are flagged (a bool must be
 *   `true`, a bound must be present).
 *
 * The three cases are mutually exclusive so a single root cause never produces
 * a wall of chips. */
export function ignoredPolicySettings(
  policy: Record<string, unknown> | null | undefined,
  caps: AgentCapabilities | null | undefined,
): IgnoredSetting[] {
  if (!policy || !caps) return [];
  const out: IgnoredSetting[] = [];
  const active = (key: string): boolean =>
    key === "extract_max_bytes"
      ? typeof policy[key] === "number"
      : policy[key] === true;
  const tools = caps.tools ?? {};
  const dependents = [
    "extract_body_text",
    "extract_ocr",
    "extract_exif",
    "extract_max_bytes",
  ];

  if (caps.extract !== true) {
    for (const key of ["extract_enabled", ...dependents]) {
      if (active(key))
        out.push({
          key,
          reason:
            "this agent advertises no extraction pass (an older build, or " +
            "extraction is unavailable on the host)",
        });
    }
    return out;
  }
  if (policy.extract_enabled !== true) {
    for (const key of dependents) {
      if (active(key))
        out.push({
          key,
          reason: "extract_enabled is not on, so the extraction pass never runs",
        });
    }
    return out;
  }
  // Rule order mirrors the agent's own IgnoredSettings (internal/config/
  // unsupported.go) exactly: the console's computed view and the agent-reported
  // one are shown in the same place, and a different order would read as a
  // disagreement between them.
  //
  // The poppler trio was added to the advertisement after the first extraction
  // build shipped, so an ABSENT key means "this agent is too old to say" while an
  // explicit false means "the host really does not have it". Only the latter is a
  // host problem worth telling the operator to fix.
  const missing = (tool: string) => tools[tool] === false;

  if (active("extract_ocr") && tools.tesseract !== true)
    out.push({ key: "extract_ocr", reason: "no tesseract on the agent host" });
  if (active("extract_ocr") && tools.tesseract === true && missing("pdftoppm"))
    out.push({
      key: "extract_ocr",
      reason:
        "no pdftoppm (poppler-utils) on the agent host — scanned PDFs are skipped; images still OCR",
    });
  if (tools.ffprobe !== true)
    out.push({
      key: "extract_enabled",
      reason:
        "no ffprobe on the agent host — the video/audio technical probe is skipped",
    });
  if (active("extract_exif") && tools.exiftool !== true)
    out.push({ key: "extract_exif", reason: "no exiftool on the agent host" });
  if (missing("pdfinfo"))
    out.push({
      key: "extract_enabled",
      reason:
        "no pdfinfo (poppler-utils) on the agent host — PDF page count and properties are skipped",
    });
  if (active("extract_body_text") && missing("pdftotext"))
    out.push({
      key: "extract_body_text",
      reason:
        "no pdftotext (poppler-utils) on the agent host — PDF text is skipped; other documents still extract",
    });
  return out;
}

// --------------------------------------------------------------------------- //
// Scope strings                                                                 //
// --------------------------------------------------------------------------- //

/** Mirror of filearr.policy.parse_scope's grammar for a friendly label. */
export function describeScope(scope: string): string {
  if (scope === "global") return "Global (every agent)";
  if (scope === "none") return "No policy document anywhere";
  const [kind, ...rest] = scope.split(":");
  const id = rest.join(":");
  if (kind === "group") return `Rollout group "${id}"`;
  if (kind === "agent") return `Agent ${id}`;
  return scope;
}

/** Human label for a `source_keys` entry from GET /agent-policies/effective. */
export const SOURCE_LABELS: Record<string, string> = {
  agent: "agent policy",
  group: "rollout-group policy",
  global: "global policy",
  "group-settings": "config group",
  default: "agent default",
};
