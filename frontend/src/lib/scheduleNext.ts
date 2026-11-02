// nextRuns — the next n fire times of a stored 5-field cron, evaluated in UTC
// exactly as the backend scheduler does (schedule.py `next_occurrence` over
// cronsim 2.7). Pure and DOM-free; ScheduleField and the report-schedule table
// format the result as local datetimes for a "Next runs" preview.
//
// Mirrors cronsim's grammar, including its quirks:
//   * fields: `*`, N, A-B, lists, `X/S` (from X to the field max), `A-B/S` and
//     `*/S` (every S-th of the sorted set); names JAN-DEC / SUN-SAT (any case);
//     day-of-week 0-7 with 7 == Sunday;
//   * day-of-month `L` (last day) and `LW` (last weekday);
//   * day-of-week `DL` (last D of the month) and `D#N` (N-th D, 1-5);
//   * Debian rule: day-of-month and day-of-week are OR-ed, unless either field
//     STARTS with `*` — then they are AND-ed (`*/2` + `MON` = Mondays on odd
//     days);
//   * a day-of-month set whose smallest day exists in none of the months is an
//     error (`0 0 30 2 *`).
// Anything cronsim rejects (and 6-field second-granular crons, which the
// builder never writes) yields []. Minute-granular like the scheduler tick:
// `now` is floored to the minute and only runs strictly after it count.

const LAST = -1;
const LAST_WEEKDAY = -2;
const DAYS_IN_MONTH = [0, 31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
const MONTH_NAMES = "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split(" ");
const DAY_NAMES = "SUN MON TUE WED THU FRI SAT".split(" ");

type FieldKind = "minute" | "hour" | "dom" | "month" | "dow";
const RANGE: Record<FieldKind, [number, number]> = {
  minute: [0, 59],
  hour: [0, 23],
  dom: [1, 31],
  month: [1, 12],
  dow: [0, 7],
};

class CronError extends Error {}

/** A day-of-week item: plain day, last-of-month, or N-th of month. */
type DowItem = { dow: number; nth?: number; last?: boolean };

function strictInt(s: string): number {
  if (!/^\d+$/.test(s)) throw new CronError(s);
  return Number(s);
}

function fieldInt(s: string, kind: FieldKind): number {
  if (kind === "month" && MONTH_NAMES.includes(s)) return MONTH_NAMES.indexOf(s) + 1;
  if (kind === "dow" && DAY_NAMES.includes(s)) return DAY_NAMES.indexOf(s);
  const n = strictInt(s);
  const [lo, hi] = RANGE[kind];
  if (n < lo || n > hi) throw new CronError(s);
  return n;
}

const range = (a: number, b: number, step = 1): number[] => {
  const out: number[] = [];
  for (let i = a; i <= b; i += step) out.push(i);
  return out;
};

/** cronsim Field.parse for the numeric fields (dom specials included). */
function parseNumeric(s: string, kind: FieldKind): Set<number> {
  const [lo, hi] = RANGE[kind];
  if (s === "*") return new Set(range(lo, hi));
  if (s.includes(",")) {
    const out = new Set<number>();
    for (const term of s.split(",")) for (const v of parseNumeric(term, kind)) out.add(v);
    return out;
  }
  if (s.includes("/")) {
    const i = s.indexOf("/");
    const step = strictInt(s.slice(i + 1));
    if (step === 0) throw new CronError(s);
    const items = parseNumeric(s.slice(0, i), kind);
    if (items.size === 1 && (items.has(LAST) || items.has(LAST_WEEKDAY))) return items;
    if (items.size === 1) {
      const [start] = items;
      return new Set(range(start, hi, step));
    }
    const sorted = [...items].sort((a, b) => a - b);
    return new Set(sorted.filter((_, idx) => idx % step === 0));
  }
  if (s.includes("-")) {
    const i = s.indexOf("-");
    const a = fieldInt(s.slice(0, i), kind);
    const b = fieldInt(s.slice(i + 1), kind);
    if (b < a) throw new CronError(s);
    return new Set(range(a, b));
  }
  if (kind === "dom" && s === "LW") return new Set([LAST_WEEKDAY]);
  if (kind === "dom" && s === "L") return new Set([LAST]);
  return new Set([fieldInt(s, kind)]);
}

/** cronsim Field.parse for day-of-week (adds `DL` and `D#N`). */
function parseDow(s: string): DowItem[] {
  if (s.includes(",")) return s.split(",").flatMap(parseDow);
  if (s.includes("L")) {
    const v = s.slice(0, -1);
    if (!/^\d+$/.test(v)) throw new CronError(s);
    return [{ dow: fieldInt(v, "dow"), last: true }];
  }
  if (s.includes("#")) {
    const i = s.indexOf("#");
    const nth = strictInt(s.slice(i + 1));
    if (nth < 1 || nth > 5) throw new CronError(s);
    return [{ dow: fieldInt(s.slice(0, i), "dow"), nth }];
  }
  return [...parseNumeric(s, "dow")].map((dow) => ({ dow }));
}

interface Spec {
  minutes: number[];
  hours: number[];
  days: Set<number>;
  months: Set<number>;
  dows: DowItem[];
  dayAnd: boolean;
}

function parseSpec(cron: string): Spec {
  const parts = cron.toUpperCase().trim().split(/\s+/);
  if (parts.length !== 5) throw new CronError(cron);
  const [mi, ho, dom, mon, dow] = parts;
  const spec: Spec = {
    minutes: [...parseNumeric(mi, "minute")].sort((a, b) => a - b),
    hours: [...parseNumeric(ho, "hour")].sort((a, b) => a - b),
    days: parseNumeric(dom, "dom"),
    months: parseNumeric(mon, "month"),
    dows: parseDow(dow),
    dayAnd: dom.startsWith("*") || dow.startsWith("*"),
  };
  const minDay = Math.min(...spec.days);
  if (minDay > 29 && minDay > Math.max(...[...spec.months].map((m) => DAYS_IN_MONTH[m]))) {
    throw new CronError(cron);
  }
  return spec;
}

const daysIn = (y: number, m: number): number => new Date(Date.UTC(y, m, 0)).getUTCDate();

function lastWeekday(y: number, m: number): number {
  const last = daysIn(y, m);
  const dow = new Date(Date.UTC(y, m - 1, last)).getUTCDay();
  if (dow === 0) return last - 2;
  if (dow === 6) return last - 1;
  return last;
}

function matchDom(spec: Spec, y: number, m: number, d: number): boolean {
  if (spec.days.has(d)) return true;
  if (spec.days.has(LAST_WEEKDAY) && d === lastWeekday(y, m)) return true;
  if (spec.days.has(LAST) && d === daysIn(y, m)) return true;
  return false;
}

function matchDow(spec: Spec, y: number, m: number, d: number): boolean {
  const js = new Date(Date.UTC(y, m - 1, d)).getUTCDay();
  const dow7 = js === 0 ? 7 : js; // cronsim: weekday()+1, matched as-is or %7
  const is = (x: number) => x === dow7 || x === js;
  const idx = Math.floor((d + 6) / 7);
  return spec.dows.some((it) => {
    if (!is(it.dow)) return false;
    if (it.last) return d + 7 > daysIn(y, m);
    if (it.nth !== undefined) return it.nth === idx;
    return true;
  });
}

function matchDay(spec: Spec, y: number, m: number, d: number): boolean {
  return spec.dayAnd
    ? matchDom(spec, y, m, d) && matchDow(spec, y, m, d)
    : matchDom(spec, y, m, d) || matchDow(spec, y, m, d);
}

// Far enough for any satisfiable expression's next run (`0 0 29 2 *` waits
// up to eight years across a skipped century leap day; MON#5 in a given month
// recurs within ~11 years), bounded so an unsatisfiable one cannot spin.
const HORIZON_DAYS = 366 * 28;

/** The next `n` run times of a UTC `cron` strictly after `now` (minute-floored). */
export function nextRuns(cron: string, n: number, now: Date): Date[] {
  if (n <= 0 || !cron || !cron.trim() || Number.isNaN(now.getTime())) return [];
  let spec: Spec;
  try {
    spec = parseSpec(cron);
  } catch (e) {
    if (e instanceof CronError) return [];
    throw e;
  }
  const after = Math.floor(now.getTime() / 60_000) * 60_000;
  const out: Date[] = [];
  const start = new Date(after);
  const day0 = Date.UTC(start.getUTCFullYear(), start.getUTCMonth(), start.getUTCDate());
  for (let i = 0; i < HORIZON_DAYS && out.length < n; i++) {
    const day = new Date(day0 + i * 86_400_000);
    const y = day.getUTCFullYear();
    const m = day.getUTCMonth() + 1;
    const d = day.getUTCDate();
    if (!spec.months.has(m) || !matchDay(spec, y, m, d)) continue;
    for (const h of spec.hours) {
      for (const mi of spec.minutes) {
        const t = day.getTime() + h * 3_600_000 + mi * 60_000;
        if (t <= after) continue;
        out.push(new Date(t));
        if (out.length >= n) return out;
      }
    }
  }
  return out;
}

/** Why a cron would be refused, or null when it looks valid. Uses the same
 *  parser as `nextRuns` (no run within the horizon = refused). A 6-field cron
 *  (cronsim's optional seconds field) is not modelled here, so it is left to
 *  the server's validation rather than refused on the client. */
export function cronProblem(cron: string, now: Date = new Date()): string | null {
  const s = (cron ?? "").trim();
  if (!s) return "Enter a cron expression (minute hour day month weekday).";
  if (s.split(/\s+/).length === 6) return null;
  if (nextRuns(s, 1, now).length > 0) return null;
  return "Not a valid cron: use five fields, minute hour day-of-month month day-of-week (e.g. 30 3 * * *).";
}
