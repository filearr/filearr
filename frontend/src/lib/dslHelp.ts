// FIX-12 (Item B) — shared "Query syntax" help for the filter DSL, consumed by
// BOTH the Search page and the Custom-reports builder so the two surfaces
// document the SAME grammar from one source (no drift, no duplication).
//
// The grammar itself is the NORMATIVE reference in backend/filearr/querydsl.py +
// shared/querydsl-vectors.json — nothing here invents syntax. Every `q:` example
// string below is verified by a backend test (test_dsl_help_examples.py): the
// test runs each example through the real parse() (must not raise) AND asserts
// each string appears verbatim in this file, so a documented example can never
// silently diverge from what the parser actually accepts.
//
// searchOnly sections (fuzzy ~terms) are grammar-valid but the reports
// translator rejects fuzzy, so the shared component labels them "not supported
// in reports" there; since P6 the Search page cannot honour them either (no ~
// in Meilisearch), so the section is shown in reports only, labelled as such.
//
// P6 (finding A2, ruling 1): the Search page does not send the DSL to a parser —
// searchDsl.ts translates only kind:/group:/ext:/size:/modified:/tag: into flat /search
// params, and -word / "phrase" ride in q. So every section and example carries
// the contexts it is true for, and dslSectionsFor("search") returns only what
// search actually does (a Node test runs each of those examples through
// dslToParams and requires zero unsupported tokens). Reports keep everything.

export type DslContext = "search" | "reports";

export interface DslExample {
  /** Literal query fragment; clicking its chip inserts this into the query box. */
  q: string;
  /** Short gloss of what it matches (chip tooltip). */
  note?: string;
  /** Where this example works; defaults to its section's contexts. */
  contexts?: DslContext[];
}

export interface DslSection {
  title: string;
  /** One-line prose description of the construct. */
  body: string;
  examples: DslExample[];
  /** Grammar-valid but search-only (fuzzy); flagged unsupported in reports. */
  searchOnly?: boolean;
  /** Surfaces whose help lists this section. */
  contexts: DslContext[];
  /** Search-context title/body when the search subset differs from the full grammar. */
  search?: { title?: string; body?: string };
}

const BOTH: DslContext[] = ["search", "reports"];
const REPORTS: DslContext[] = ["reports"];

export const DSL_SECTIONS: DslSection[] = [
  {
    title: "Free text",
    contexts: BOTH,
    body:
      "Bare words match the filename, title and path. Several words all have to " +
      "match (AND). Wrap a phrase in double quotes to keep its spaces together.",
    examples: [
      { q: "invoice", note: "files mentioning invoice" },
      { q: '"annual report"', note: "the exact phrase" },
      { q: '"quarterly report"', note: "another quoted phrase" },
    ],
  },
  {
    title: "Fuzzy terms",
    searchOnly: true,
    // Meilisearch has no ~ operator, so the Search page cannot honour it either.
    contexts: REPORTS,
    body:
      "Prefix a term with ~ for typo-tolerant matching (free-text terms only, never " +
      "filters). The grammar accepts it, but neither search nor reports can honour it yet.",
    examples: [{ q: "~documentaru", note: "matches ‘documentary’ despite the typo" }],
  },
  {
    title: "Negation",
    contexts: BOTH,
    body: "Prefix any term or filter with - or ! to exclude it.",
    search: { body: "Prefix a word with - to exclude results containing it." },
    examples: [
      { q: "-draft", note: "exclude the word draft" },
      { q: "-kind:sample", note: "exclude sample-type items", contexts: REPORTS },
      { q: "!ext:tmp", note: "exclude .tmp files", contexts: REPORTS },
    ],
  },
  {
    title: "kind:",
    contexts: BOTH,
    body: "Filter by media type (video, audio, image, document, …).",
    examples: [
      { q: "kind:video" },
      { q: "kind:audio" },
    ],
  },
  {
    title: "group:",
    contexts: BOTH,
    body:
      "Filter by the finer file group (raw-photo, audio-lossless, pdf, archive, …) " +
      "— the granular child of kind.",
    examples: [
      { q: "group:raw-photo" },
      { q: "group:audio-lossless" },
    ],
  },
  {
    title: "ext:",
    contexts: BOTH,
    body:
      "Filter by file extension. List several with ; — the leading dot is optional " +
      "and matching is case-insensitive.",
    search: {
      body:
        "Filter by one file extension — the leading dot is optional and matching " +
        "is case-insensitive.",
    },
    examples: [
      { q: "ext:pdf" },
      { q: "ext:mp4;mkv;avi", note: "any of these extensions", contexts: REPORTS },
    ],
  },
  {
    title: "size:",
    contexts: BOTH,
    body:
      "Filter by file size with a comparator (>, >=, <, <=, =) or an A..B range. " +
      "Units K/M/G/T are binary (1024-based); no unit means bytes.",
    examples: [
      { q: "size:>1G", note: "larger than 1 GiB" },
      { q: "size:<500K", note: "smaller than 500 KiB" },
      { q: "size:100M..4G", note: "inclusive range" },
    ],
  },
  {
    title: "modified: / created:",
    contexts: BOTH,
    body:
      "Filter by modified or created time. Use a relative duration (s, m, h, d, w) " +
      "or an ISO date (YYYY-MM-DD), with a comparator or an A..B range.",
    search: {
      title: "modified:",
      body:
        "Filter by modified time: a comparator (>, >=, <, <=) with a relative " +
        "duration (s, m, h, d, w), an ISO date (YYYY-MM-DD) with or without one, " +
        "or a date range such as modified:2024-01-01..2024-12-31 (both days included).",
    },
    examples: [
      { q: "modified:>7d", note: "changed in the last 7 days" },
      { q: "modified:<30d", note: "not touched for 30 days" },
      { q: "modified:>=2025-01-01", note: "on/after a date" },
      { q: "created:2024-01-01..2024-12-31", note: "created within 2024", contexts: REPORTS },
    ],
  },
  {
    title: "path:",
    contexts: REPORTS,
    body: "Match a path glob (kept verbatim). Quote it if it contains spaces.",
    examples: [
      { q: "path:*/backups/*" },
      { q: 'path:"*/Season 01/*"', note: "glob with a space" },
    ],
  },
  {
    title: "tag:",
    contexts: BOTH,
    body: "Filter by tag name (combine with - to exclude a tag).",
    search: { body: "Filter by tag name; several tag: filters must all match." },
    examples: [
      { q: "tag:archived" },
      { q: "-tag:draft", note: "exclude a tag", contexts: REPORTS },
    ],
  },
  {
    title: "hash:",
    contexts: REPORTS,
    body: "Match a content-hash digest or a hex prefix.",
    examples: [{ q: "hash:e3b0c442", note: "digest prefix" }],
  },
  {
    title: "meta.<key>: and cf.<name>:",
    contexts: REPORTS,
    body:
      "Filter on an extracted metadata value (meta.<key>) or a registered custom " +
      "field (cf.<name>). Both accept a comparator or an A..B range. Keys are " +
      "lowercase [a-z0-9_], dot-separated for nested metadata.",
    examples: [
      { q: "meta.height:>=1080", note: "1080p and up" },
      { q: "meta.duration:>3600", note: "longer than an hour" },
      { q: "meta.width:1920..3840", note: "width range" },
      { q: "cf.rating:>=4", note: "custom field ≥ 4" },
      { q: "cf.shelf_location:A12", note: "exact custom-field value" },
    ],
  },
  {
    title: "Combine",
    contexts: REPORTS,
    body: "Filters and free text combine with AND — mix as many as you like.",
    examples: [
      { q: "kind:video meta.height:>=1080 -tag:archived", note: "HD, not archived" },
      { q: "kind:audio ext:flac size:>50M", note: "large FLAC audio" },
    ],
  },
];

/** The sections (and examples) that are true for one surface. Reports get the
 *  full grammar unchanged; Search gets only what searchDsl.ts translates. */
export function dslSectionsFor(context: DslContext): DslSection[] {
  return DSL_SECTIONS.filter((sec) => sec.contexts.includes(context)).map((sec) => {
    const examples = sec.examples.filter((ex) => (ex.contexts ?? sec.contexts).includes(context));
    const over = context === "search" ? sec.search : undefined;
    return {
      ...sec,
      title: over?.title ?? sec.title,
      body: over?.body ?? sec.body,
      examples,
    };
  });
}
