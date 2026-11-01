// The one place bytes, dates, durations and relative times become display
// strings (review 2, A32). Pure and DOM-free so Node tests import it directly.
//
// Conventions every page shares:
//   - absent / invalid input renders as an em dash "—", never "0 B" or "NaN";
//   - bytes are base 1024 with plain unit names (B KB MB GB TB PB);
//   - dates are the viewer's LOCAL calendar date, except fmtUtc (tooltips).

const DASH = "—";
const UNITS = ["B", "KB", "MB", "GB", "TB", "PB"];

/** Human byte size: `512 B`, `1 KB`, `1.5 KB`, `2 MB`. Zero prints
 *  `opts.zero ?? "0 B"`; null, non-finite and negative print "—". */
export function fmtBytes(n: number | null | undefined, opts?: { zero?: string }): string {
  if (n == null || !Number.isFinite(n) || n < 0) return DASH;
  if (n === 0) return opts?.zero ?? "0 B";
  if (n < 1024) {
    const r = Math.round(n);
    // 1023.6 rounds to 1024: that is a kilobyte, not "1024 B".
    return r < 1024 ? `${r} B` : "1 KB";
  }
  let i = 0;
  let v = n;
  while (v >= 1024 && i < UNITS.length - 1) {
    v /= 1024;
    i++;
  }
  let s = v.toFixed(1);
  // Rounding can carry into the next unit ("1024.0 KB" -> "1 MB").
  if (Number(s) >= 1024 && i < UNITS.length - 1) {
    i++;
    s = (v / 1024).toFixed(1);
  }
  return `${s.endsWith(".0") ? s.slice(0, -2) : s} ${UNITS[i]}`;
}

type DateInput = number | string | Date | null | undefined;

const EXIF = /^(\d{4}):(\d{2}):(\d{2})(?:[ T](\d{2}):(\d{2}):(\d{2}))?$/;
const ISO_DATE_ONLY = /^(\d{4})-(\d{2})-(\d{2})$/;

/** Epoch milliseconds for any accepted input, or null when it is not a date.
 *  Numbers below 1e12 are epoch seconds (1e12 s is the year 33658), else ms.
 *  EXIF `YYYY:MM:DD HH:MM:SS` and date-only ISO carry no zone: local wall time. */
function toMs(v: DateInput): number | null {
  if (v == null) return null;
  if (v instanceof Date) {
    const t = v.getTime();
    return Number.isFinite(t) ? t : null;
  }
  if (typeof v === "number") {
    if (!Number.isFinite(v)) return null;
    return Math.abs(v) < 1e12 ? v * 1000 : v;
  }
  const s = v.trim();
  if (!s) return null;
  const m = EXIF.exec(s) ?? ISO_DATE_ONLY.exec(s);
  if (m) {
    const [y, mo, d, h = 0, mi = 0, se = 0] = m.slice(1).map((x) => (x == null ? 0 : Number(x)));
    if (mo < 1 || mo > 12 || d < 1 || d > 31 || h > 23 || mi > 59 || se > 59) return null;
    const dt = new Date(y, mo - 1, d, h, mi, se);
    // Reject roll-over dates such as Feb 31.
    if (dt.getMonth() !== mo - 1) return null;
    return dt.getTime();
  }
  const t = Date.parse(s);
  return Number.isFinite(t) ? t : null;
}

const pad = (n: number): string => String(n).padStart(2, "0");

function localDate(d: Date): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** Local calendar date `YYYY-MM-DD`; "—" when not a date. */
export function fmtDate(v: DateInput): string {
  const t = toMs(v);
  return t == null ? DASH : localDate(new Date(t));
}

/** Local `YYYY-MM-DD HH:mm`; "—" when not a date. */
export function fmtDateTime(v: DateInput): string {
  const t = toMs(v);
  if (t == null) return DASH;
  const d = new Date(t);
  return `${localDate(d)} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** UTC `YYYY-MM-DDTHH:mm:ssZ`, for `title` tooltips; "—" when not a date. */
export function fmtUtc(v: DateInput): string {
  const t = toMs(v);
  return t == null ? DASH : `${new Date(t).toISOString().slice(0, 19)}Z`;
}

/** Elapsed seconds: `0s`, `59s`, `1m 5s`, `1h 2m`, `25h 0m` (no days).
 *  Rounded to whole seconds; negative, non-finite and absent print "—". */
export function fmtDuration(sec: number | null | undefined): string {
  if (sec == null || !Number.isFinite(sec) || sec < 0) return DASH;
  const s = Math.round(sec);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

/** Relative to `now` (ms): `just now` (< 45 s), `N min ago`, `N h ago`,
 *  `yesterday`, `N days ago`, then the local date from 30 days on. Future
 *  instants read `in N min`, `in N h`, `tomorrow`, `in N days`. */
export function relTime(v: DateInput, now: number = Date.now()): string {
  const t = toMs(v);
  if (t == null) return DASH;
  const diff = (now - t) / 1000;
  const a = Math.abs(diff);
  const past = diff >= 0;
  if (a < 45) return "just now";
  const mins = Math.round(a / 60);
  if (mins < 60) return past ? `${mins} min ago` : `in ${mins} min`;
  const hours = Math.round(a / 3600);
  if (hours < 24) return past ? `${hours} h ago` : `in ${hours} h`;
  const days = Math.round(a / 86400);
  if (days === 1) return past ? "yesterday" : "tomorrow";
  if (days < 30) return past ? `${days} days ago` : `in ${days} days`;
  return localDate(new Date(t));
}
