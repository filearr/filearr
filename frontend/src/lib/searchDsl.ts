// Query DSL -> flat /search params (finding A2, ruling 1).
//
// `/search` hands `q` to Meilisearch verbatim; the backend has no DSL parser on
// that path. So the Search page translates the operators the backend CAN express
// as flat params here, client-side, and reports the rest:
//
//   kind:V            -> file_category (repeatable, OR)
//   group:V           -> file_group (repeatable, OR)
//   ext:V             -> extension (single; a second value is unsupported)
//   size:>N / A..B    -> size_gte / size_lte (inclusive integer bytes)
//   modified:>7d / D  -> mtime_gte / mtime_lte (inclusive epoch seconds)
//   tag:V             -> tags (comma-joined, AND)
//
// `-word` and `"phrase"` stay in `q` (Meilisearch handles both). Everything else
// the grammar knows (meta.*, cf.*, path:, created:, hash:, negated
// filters, ~fuzzy) cannot be expressed by /search and is returned in
// `unsupported` and removed from `q`, so it never reaches Meilisearch as a
// literal word that matches nothing.
//
// Tokenising mirrors backend/filearr/querydsl.py: whitespace splits tokens,
// double quotes keep spaces (and colons) together, the first UNQUOTED colon after
// an unquoted key makes a filter, known keys match case-insensitively. Pure and
// DOM-free (Node-tested).

export type DslParamKey =
  | "file_category"
  | "file_group"
  | "extension"
  | "size_gte"
  | "size_lte"
  | "mtime_gte"
  | "mtime_lte"
  | "tags";

export interface DslResult {
  /** The input minus translated and unsupported tokens, single-spaced. */
  q: string;
  params: Partial<Record<DslParamKey, string>>;
  /** Raw tokens (or `ext:<value>` for an extra extension) search cannot express. */
  unsupported: string[];
}

const KEYS = new Set(["kind", "group", "ext", "size", "modified", "created", "path", "tag", "hash"]);
const DYNAMIC = /^(meta|cf)\./;
const SIZE_UNIT: Record<string, number> = { "": 1, K: 1024, M: 1024 ** 2, G: 1024 ** 3, T: 1024 ** 4 };
const DURATION_UNIT: Record<string, number> = { s: 1, m: 60, h: 3600, d: 86_400, w: 604_800 };

const SIZE_RE = /^(\d+)([KMGT]?)$/i;
const DURATION_RE = /^(\d+)([smhdw])$/;
const DATE_RE = /^(\d{4})-(\d{2})-(\d{2})$/;

/** One lexed token: the raw text plus, per character of the unquoted reading,
 *  whether it sat inside double quotes (quote marks themselves are dropped). */
interface Token {
  raw: string;
  chars: { c: string; quoted: boolean }[];
}

function lex(s: string): Token[] {
  const out: Token[] = [];
  let cur: Token | null = null;
  let inQuote = false;
  for (const c of s) {
    if (inQuote) {
      cur!.raw += c;
      if (c === '"') inQuote = false;
      else cur!.chars.push({ c, quoted: true });
      continue;
    }
    if (/\s/.test(c)) {
      if (cur) out.push(cur);
      cur = null;
      continue;
    }
    cur ??= { raw: "", chars: [] };
    cur.raw += c;
    if (c === '"') inQuote = true;
    else cur.chars.push({ c, quoted: false });
  }
  if (cur) out.push(cur);
  return out;
}

type Comparator = ">" | ">=" | "<" | "<=" | "=";

function readComparator(v: string): [Comparator | null, string] {
  const two = v.slice(0, 2);
  if (two === ">=" || two === "<=") return [two, v.slice(2)];
  const one = v.slice(0, 1);
  if (one === ">" || one === "<" || one === "=") return [one, v.slice(1)];
  return [null, v];
}

function sizeNum(s: string): number | null {
  const m = SIZE_RE.exec(s);
  if (!m) return null;
  const n = Number(m[1]) * SIZE_UNIT[m[2].toUpperCase()];
  return Number.isSafeInteger(n) ? n : null;
}

/** Inclusive [gte, lte] byte bounds for a size value, or null when untranslatable. */
function sizeBounds(v: string): [number | null, number | null] | null {
  const [op, rest] = readComparator(v);
  let lo: number | null = null;
  let hi: number | null = null;
  if (op === null && v.includes("..")) {
    const parts = v.split("..");
    if (parts.length !== 2) return null;
    lo = sizeNum(parts[0]);
    hi = sizeNum(parts[1]);
    if (lo === null || hi === null) return null;
  } else {
    const n = sizeNum(rest);
    if (n === null) return null;
    if (op === ">") lo = n + 1;
    else if (op === ">=") lo = n;
    else if (op === "<") hi = n - 1;
    else if (op === "<=") hi = n;
    else lo = hi = n;
  }
  if ((hi !== null && hi < 0) || (lo !== null && !Number.isSafeInteger(lo))) return null;
  return [lo, hi];
}

/** Local midnight (epoch seconds) of a YYYY-MM-DD date, or null if not a real date. */
function localMidnight(s: string): number | null {
  const m = DATE_RE.exec(s);
  if (!m) return null;
  const [y, mo, d] = [Number(m[1]), Number(m[2]), Number(m[3])];
  const dt = new Date(y, mo - 1, d);
  if (dt.getFullYear() !== y || dt.getMonth() !== mo - 1 || dt.getDate() !== d) return null;
  return Math.floor(dt.getTime() / 1000);
}

/** Local midnight of the day after a date (exclusive end of that day). */
function nextMidnight(s: string): number | null {
  if (localMidnight(s) === null) return null;
  const [y, mo, d] = s.split("-").map(Number);
  return Math.floor(new Date(y, mo - 1, d + 1).getTime() / 1000);
}

/** Inclusive [gte, lte] epoch-second bounds for a modified: value. */
function mtimeBounds(v: string, nowS: number): [number | null, number | null] | null {
  const [op, rest] = readComparator(v);
  if (op === null && v.includes("..")) {
    const parts = v.split("..");
    if (parts.length !== 2) return null;
    const lo = localMidnight(parts[0]);
    const end = nextMidnight(parts[1]);
    return lo === null || end === null ? null : [lo, end - 1];
  }
  const dur = DURATION_RE.exec(rest);
  if (dur) {
    // ">7d" = changed within the last 7 days; "<30d" = older than 30 days.
    const t = nowS - Number(dur[1]) * DURATION_UNIT[dur[2]];
    if (op === ">" || op === ">=") return [t, null];
    if (op === "<" || op === "<=") return [null, t];
    return null; // "=7d" / bare "7d" has no useful meaning as a range
  }
  const day = localMidnight(rest);
  const end = nextMidnight(rest);
  if (day === null || end === null) return null;
  if (op === ">") return [end, null];
  if (op === ">=") return [day, null];
  if (op === "<") return [null, day - 1];
  if (op === "<=") return [null, end - 1];
  return [day, end - 1]; // "=D" or bare "D": that whole local day
}

export function dslToParams(q: string, now: number = Date.now()): DslResult {
  const nowS = Math.floor(now / 1000);
  const kept: string[] = [];
  const unsupported: string[] = [];
  const categories: string[] = [];
  const groups: string[] = [];
  const tags: string[] = [];
  let extension: string | null = null;
  let sizeLo: number | null = null;
  let sizeHi: number | null = null;
  let mtimeLo: number | null = null;
  let mtimeHi: number | null = null;

  const maxOf = (a: number | null, b: number | null) => (a === null ? b : b === null ? a : Math.max(a, b));
  const minOf = (a: number | null, b: number | null) => (a === null ? b : b === null ? a : Math.min(a, b));
  const addUnique = (list: string[], v: string) => {
    if (!list.includes(v)) list.push(v);
  };

  for (const tok of lex(q)) {
    let chars = tok.chars;
    let negated = false;
    let fuzzy = false;
    if (chars[0] && !chars[0].quoted && (chars[0].c === "-" || chars[0].c === "!")) {
      negated = true;
      chars = chars.slice(1);
    }
    if (chars[0] && !chars[0].quoted && chars[0].c === "~") {
      fuzzy = true;
      chars = chars.slice(1);
    }
    // A fuzzy term (~word): Meilisearch has no "~" operator, so search cannot
    // honour it. A lone "~" is just a character.
    if (fuzzy && chars.length) {
      unsupported.push(tok.raw);
      continue;
    }
    const colon = chars.findIndex((ch) => ch.c === ":" && !ch.quoted);
    const keyChars = colon > 0 ? chars.slice(0, colon) : [];
    const key = keyChars.map((ch) => ch.c).join("");
    const isFilter =
      colon > 0 && keyChars.every((ch) => !ch.quoted) && (KEYS.has(key.toLowerCase()) || DYNAMIC.test(key));
    if (!isFilter) {
      kept.push(tok.raw);
      continue;
    }
    const lkey = key.toLowerCase();
    const value = chars.slice(colon + 1).map((ch) => ch.c).join("");
    if (negated || value === "") {
      unsupported.push(tok.raw);
      continue;
    }
    if (lkey === "kind" || lkey === "group") {
      const v = value.toLowerCase();
      // Taxonomy keys are admin-editable; the backend ignores unknown ones. Only a
      // comma cannot ride the comma-joined wire value.
      if (v.includes(",")) unsupported.push(tok.raw);
      else addUnique(lkey === "kind" ? categories : groups, v);
    } else if (lkey === "ext") {
      const parts = value.toLowerCase().split(";").map((p) => p.replace(/^\.+/, ""));
      if (parts.some((p) => p === "" || p.includes(","))) {
        unsupported.push(tok.raw);
        continue;
      }
      for (const p of parts) {
        if (extension === null) extension = p;
        else if (p !== extension) unsupported.push(`ext:${p}`);
      }
    } else if (lkey === "size") {
      const b = sizeBounds(value);
      if (!b) {
        unsupported.push(tok.raw);
        continue;
      }
      sizeLo = maxOf(sizeLo, b[0]);
      sizeHi = minOf(sizeHi, b[1]);
    } else if (lkey === "modified") {
      const b = mtimeBounds(value, nowS);
      if (!b) {
        unsupported.push(tok.raw);
        continue;
      }
      mtimeLo = maxOf(mtimeLo, b[0]);
      mtimeHi = minOf(mtimeHi, b[1]);
    } else if (lkey === "tag") {
      // The wire format is comma-joined (ruling 3): a comma cannot be expressed.
      if (value.includes(",")) unsupported.push(tok.raw);
      else addUnique(tags, value);
    } else {
      unsupported.push(tok.raw); // meta.* cf.* path: created: hash:
    }
  }

  const params: DslResult["params"] = {};
  if (categories.length) params.file_category = categories.join(",");
  if (groups.length) params.file_group = groups.join(",");
  if (extension !== null) params.extension = extension;
  if (sizeLo !== null) params.size_gte = String(sizeLo);
  if (sizeHi !== null) params.size_lte = String(sizeHi);
  if (mtimeLo !== null) params.mtime_gte = String(mtimeLo);
  if (mtimeHi !== null) params.mtime_lte = String(mtimeHi);
  if (tags.length) params.tags = tags.join(",");
  return { q: kept.join(" "), params, unsupported };
}

const LIST_KEYS = ["file_category", "file_group", "tags"] as const;
const SCALAR_KEYS = ["extension", "size_gte", "size_lte", "mtime_gte", "mtime_lte"] as const;

const splitList = (v: string | undefined): string[] =>
  v ? v.split(",").map((t) => t.trim()).filter(Boolean) : [];

/** Merge a DSL translation into the Search page's chip/slider params.
 *  - No DSL token at all: `base` is returned as-is (byte-identical params).
 *  - `q` becomes the translated remainder.
 *  - `file_category` / `file_group` / `tags` union, chip values first, de-duplicated.
 *  - `extension` and the range bounds from the DSL win over chips / sliders. */
export function mergeDslParams(base: Record<string, string>, dsl: DslResult): Record<string, string> {
  if (!Object.keys(dsl.params).length && !dsl.unsupported.length) return base;
  const p: Record<string, string> = { ...base, q: dsl.q };
  for (const k of LIST_KEYS) {
    const extra = dsl.params[k];
    if (!extra) continue;
    const all = splitList(base[k]);
    for (const v of splitList(extra)) if (!all.includes(v)) all.push(v);
    p[k] = all.join(",");
  }
  for (const k of SCALAR_KEYS) {
    const v = dsl.params[k];
    if (v !== undefined) p[k] = v;
  }
  return p;
}
