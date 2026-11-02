// The one place a canned report's on-screen state (library filter, day
// threshold, page offset) becomes request parameters. DOM-free and
// Node-tested.
//
// B2 / Ruling 11: the streaming endpoint reads `limit` as a ROW CAP on an
// uncapped report ("omit limit = whole result"), so an export sends `limit`
// only for a capped report, where it is the report's definitional top-N (the
// rows the page shows). A page run always sends its page size and offset.

export type ReportQueryMeta = {
  is_capped: boolean;
  default_limit: number;
  supports_library: boolean;
  supports_threshold: boolean;
};

export type ReportQueryOpts = {
  /** `page`: one JSON page; `export`: a streaming download or background export. */
  mode: "page" | "export";
  libraryId?: string;
  thresholdDays?: number | null;
  offset?: number;
};

/** Same field names as api.ts `ReportRunOpts`. */
export type ReportQueryParams = {
  limit?: number;
  offset?: number;
  libraryId?: string;
  thresholdDays?: number;
};

/** The backend's threshold bounds (`_check_threshold`: 1..36500 days). */
const THRESHOLD_MAX = 36500;

function threshold(v: number | null | undefined): number | undefined {
  if (v == null || !Number.isFinite(v)) return undefined;
  const d = Math.round(v);
  return d >= 1 && d <= THRESHOLD_MAX ? d : undefined;
}

export function reportQuery(meta: ReportQueryMeta, opts: ReportQueryOpts): ReportQueryParams {
  const out: ReportQueryParams = {};
  if (opts.mode === "page") {
    out.limit = meta.default_limit;
    out.offset = Math.max(0, opts.offset ?? 0);
  } else if (meta.is_capped) {
    out.limit = meta.default_limit;
  }
  if (meta.supports_library && opts.libraryId) out.libraryId = opts.libraryId;
  if (meta.supports_threshold) {
    const t = threshold(opts.thresholdDays);
    if (t !== undefined) out.thresholdDays = t;
  }
  return out;
}

/** A report schedule's stored `params` (the same keys `export.params` uses, so
 *  the scheduled run reproduces what the Reports page shows). */
export function reportScheduleParams(
  meta: ReportQueryMeta,
  opts: Omit<ReportQueryOpts, "mode" | "offset">,
): Record<string, unknown> {
  const q = reportQuery(meta, { ...opts, mode: "export" });
  const out: Record<string, unknown> = {};
  if (q.limit !== undefined) out.limit = q.limit;
  if (q.libraryId !== undefined) out.library_id = q.libraryId;
  if (q.thresholdDays !== undefined) out.threshold_days = q.thresholdDays;
  return out;
}

export type EditedScheduleParams =
  | { ok: true; params: Record<string, unknown> }
  | { ok: false; error: string };

/** A report schedule's params after the Edit panel: the STORED params with only
 *  the fields the panel owns applied. `limit` (no field for it) and unknown keys
 *  are kept verbatim — re-deriving them from the report's metadata would reset
 *  a stored cap. A blank threshold (`null`) removes the key, so the server's
 *  default applies rather than the page default being pinned; a bad value is
 *  an error. Fields the report does not support are left as stored. */
export function editedScheduleParams(
  stored: Record<string, unknown>,
  meta: ReportQueryMeta,
  edits: { libraryId: string; thresholdDays: number | null | undefined },
): EditedScheduleParams {
  const out: Record<string, unknown> = { ...stored };
  if (meta.supports_library) {
    if (edits.libraryId) out.library_id = edits.libraryId;
    else delete out.library_id;
  }
  if (meta.supports_threshold) {
    const v = edits.thresholdDays;
    if (v == null) {
      delete out.threshold_days;
    } else if (!Number.isInteger(v) || v < 1 || v > THRESHOLD_MAX) {
      return { ok: false, error: `Threshold must be a whole number of days from 1 to ${THRESHOLD_MAX}, or blank for the default.` };
    } else {
      out.threshold_days = v;
    }
  }
  return { ok: true, params: out };
}
