// UI-T11 — pure cron generate/parse layer for the friendly schedule builder.
//
// Storage stays a 5-field cron string evaluated in UTC by the backend
// (schedule.py `cron_is_due`, T5 decision). This module is the ONLY place that
// knows how to turn a human "run daily at 14:00 local" choice into that UTC cron
// and back again — ScheduleField.svelte is a thin view over these functions.
//
// Timezone contract: the builder speaks LOCAL wall-clock; cron is fixed-UTC.
// `offsetMinutes` is JS `Date.prototype.getTimezoneOffset()` semantics —
// positive WEST of UTC (e.g. New York EST = +300), so `UTC = local + offset`.
// The conversion can move the day (a late-evening local run lands next-day UTC,
// an early-morning local run lands previous-day UTC); for Weekly/Monthly we shift
// the cron's day-of-week / day-of-month field to match. Because cron is stored at
// a FIXED UTC offset, the local wall-time drifts ±1h across DST — that caveat is
// surfaced in the UI, not corrected here (correcting it would need a tz database).
//
// Contract pinned language-neutrally by shared/schedule-vectors.json, checked
// by tests/schedule.node.test.ts here and backend/tests/
// test_schedule_vectors_uit11.py (cronsim + cron_is_due). Round-trip invariant:
// generateCron(parseCron(x)) === x for every cron this module generates.
//
// Guards (P7, Ruling 10): `generateCron` is the raw conversion arithmetic; the
// UI goes through `scheduleCron`, which refuses what must not be stored and
// says why — a weekly schedule with no day (B18), a missing/out-of-range
// number (B32), and a MONTHLY time whose UTC conversion crosses midnight (B19):
// a day-of-month cannot be shifted faithfully (local day 1 at 01:00 in UTC+2
// would become UTC day 31 and skip every short month), so the user picks a
// same-day time or writes the UTC cron in Advanced.

import { numOrNull } from "./numInput.ts";

export type ScheduleMode =
  | "off"
  | "hourly"
  | "daily"
  | "weekly"
  | "monthly"
  | "advanced";

export interface Schedule {
  mode: ScheduleMode;
  /** hourly: minute-past-the-hour (local). null = the input is empty. */
  minute?: number | null;
  /** daily/weekly/monthly: local hour (0-23). null = the input is empty. */
  hour?: number | null;
  /** daily/weekly/monthly: local minute (0-59) — also `minute` on hourly. */
  daysOfWeek?: number[]; // weekly: 0=Sun … 6=Sat (local)
  dayOfMonth?: number; // monthly: 1-31 (local)
  /** advanced: raw cron string as typed (UTC, opaque). */
  cron?: string;
}

/** Current local UTC offset in getTimezoneOffset() convention (+west). */
export function getOffsetMinutes(): number {
  return new Date().getTimezoneOffset();
}

const mod = (n: number, m: number): number => ((n % m) + m) % m;

/** day-of-month wrapped into the cron range 1..31. */
const wrapDom = (d: number): number => mod(d - 1, 31) + 1;

/** Strict single non-negative integer within [min,max]; null otherwise. */
function intField(s: string, min: number, max: number): number | null {
  if (!/^\d+$/.test(s)) return null;
  const n = Number(s);
  return n >= min && n <= max ? n : null;
}

interface UtcTime {
  utcHour: number;
  utcMinute: number;
  dayShift: number; // days added going local -> UTC (-1, 0, or +1)
}

/** Local wall-clock (hour:minute) -> UTC, tracking any day rollover. */
export function localTimeToUtc(
  hour: number,
  minute: number,
  offsetMinutes: number,
): UtcTime {
  const raw = hour * 60 + minute + offsetMinutes;
  const dayShift = Math.floor(raw / 1440);
  const tod = mod(raw, 1440);
  return { utcHour: Math.floor(tod / 60), utcMinute: tod % 60, dayShift };
}

interface LocalTime {
  hour: number;
  minute: number;
  dayShift: number; // days added going UTC -> local (== -localTimeToUtc.dayShift)
}

/** UTC (hour:minute) -> local wall-clock, tracking any day rollover. */
export function utcTimeToLocal(
  hour: number,
  minute: number,
  offsetMinutes: number,
): LocalTime {
  const raw = hour * 60 + minute - offsetMinutes;
  const dayShift = Math.floor(raw / 1440);
  const tod = mod(raw, 1440);
  return { hour: Math.floor(tod / 60), minute: tod % 60, dayShift };
}

const sortedUniq = (xs: number[]): number[] =>
  [...new Set(xs)].sort((a, b) => a - b);

/**
 * Build the UTC cron string for a LOCAL schedule. Returns null for "off" (and
 * for an empty Weekly / blank Advanced, which mean the same thing). Emits a
 * canonical form (plain integers, ascending unique DOW) so the round-trip
 * invariant holds.
 */
export function generateCron(
  s: Schedule,
  offsetMinutes: number = getOffsetMinutes(),
): string | null {
  switch (s.mode) {
    case "off":
      return null;
    case "hourly": {
      const m = s.minute ?? 0;
      const utcMinute = mod(m + offsetMinutes, 60);
      return `${utcMinute} * * * *`;
    }
    case "daily": {
      const { utcHour, utcMinute } = localTimeToUtc(
        s.hour ?? 0,
        s.minute ?? 0,
        offsetMinutes,
      );
      return `${utcMinute} ${utcHour} * * *`;
    }
    case "weekly": {
      const days = s.daysOfWeek ?? [];
      if (days.length === 0) return null;
      const { utcHour, utcMinute, dayShift } = localTimeToUtc(
        s.hour ?? 0,
        s.minute ?? 0,
        offsetMinutes,
      );
      const utcDows = sortedUniq(days.map((d) => mod(d + dayShift, 7)));
      return `${utcMinute} ${utcHour} * * ${utcDows.join(",")}`;
    }
    case "monthly": {
      const { utcHour, utcMinute, dayShift } = localTimeToUtc(
        s.hour ?? 0,
        s.minute ?? 0,
        offsetMinutes,
      );
      const utcDom = wrapDom((s.dayOfMonth ?? 1) + dayShift);
      return `${utcMinute} ${utcHour} ${utcDom} * *`;
    }
    case "advanced": {
      const raw = (s.cron ?? "").trim();
      return raw ? raw : null;
    }
  }
}

export type ScheduleIssueCode =
  | "no-days"
  | "monthly-day-shift"
  | "minute-required"
  | "minute-range"
  | "time-required"
  | "time-range"
  | "day-range"
  | "cron-required";

export interface ScheduleIssue {
  code: ScheduleIssueCode;
  /** One sentence for the field error, rendered as text. */
  message: string;
}

const isInt = (n: unknown, min: number, max: number): n is number =>
  typeof n === "number" && Number.isInteger(n) && n >= min && n <= max;

/**
 * The LOCAL times whose UTC conversion stays on the same calendar day — the
 * only times a monthly schedule accepts (Ruling 10). Always non-empty for real
 * offsets (|offset| < 24h).
 */
export function monthlyWindow(offsetMinutes: number): { from: string; to: string } {
  const from = Math.max(0, -offsetMinutes);
  const to = Math.min(1439, 1439 - offsetMinutes);
  return {
    from: fmtTime(Math.floor(from / 60), from % 60),
    to: fmtTime(Math.floor(to / 60), to % 60),
  };
}

/** Why the builder must not store this schedule, or null when it may. */
export function scheduleIssue(
  s: Schedule,
  offsetMinutes: number = getOffsetMinutes(),
): ScheduleIssue | null {
  switch (s.mode) {
    case "off":
      return null;
    case "advanced":
      return (s.cron ?? "").trim()
        ? null
        : { code: "cron-required", message: "Enter a cron expression, or pick Off for no schedule." };
    case "hourly":
      if (s.minute == null) {
        return { code: "minute-required", message: "Enter the minute past the hour (0-59)." };
      }
      return isInt(s.minute, 0, 59)
        ? null
        : { code: "minute-range", message: "The minute must be a whole number from 0 to 59." };
  }
  // daily / weekly / monthly share a local time of day.
  if (s.hour == null || s.minute == null) {
    return { code: "time-required", message: "Enter a time of day." };
  }
  if (!isInt(s.hour, 0, 23) || !isInt(s.minute, 0, 59)) {
    return { code: "time-range", message: "Enter a valid time of day (00:00-23:59)." };
  }
  if (s.mode === "weekly" && (s.daysOfWeek ?? []).length === 0) {
    return { code: "no-days", message: "Pick at least one day, or choose Off for no schedule." };
  }
  if (s.mode === "monthly") {
    const day = s.dayOfMonth ?? 1;
    if (!isInt(day, 1, 31)) {
      return { code: "day-range", message: "The day of the month must be from 1 to 31." };
    }
    const { utcHour, utcMinute, dayShift } = localTimeToUtc(s.hour, s.minute, offsetMinutes);
    if (dayShift !== 0) {
      const { from, to } = monthlyWindow(offsetMinutes);
      const when = dayShift < 0 ? "the previous day" : "the next day";
      return {
        code: "monthly-day-shift",
        message:
          `${fmtTime(s.hour, s.minute)} local is ${fmtTime(utcHour, utcMinute)} UTC on ${when}, ` +
          `so the stored schedule would run on a different day of the month (and skip short months). ` +
          `Pick a time from ${from} to ${to}, or use Advanced (cron) to write the UTC schedule yourself.`,
      };
    }
  }
  return null;
}

/**
 * The guarded builder the UI stores from: the UTC cron, or null with the
 * reason it must not be stored. `off` is `{ cron: null, issue: null }`.
 */
export function scheduleCron(
  s: Schedule,
  offsetMinutes: number = getOffsetMinutes(),
): { cron: string | null; issue: ScheduleIssue | null } {
  const issue = scheduleIssue(s, offsetMinutes);
  if (issue) return { cron: null, issue };
  return { cron: generateCron(s, offsetMinutes), issue: null };
}

/**
 * A minute input's value (number from a bound <input type="number">, or a
 * string) rounded and clamped to 0-59; null when blank or not a number, so an
 * emptied field is an explicit error rather than a silent minute 0 (B32).
 */
export function parseMinuteInput(v: unknown): number | null {
  const n = numOrNull(v);
  if (n === null) return null;
  return Math.min(59, Math.max(0, Math.round(n)));
}

/** "HH:MM" (or "HH:MM:SS") from a time input; null when blank or invalid. */
export function parseTimeInput(v: unknown): { hour: number; minute: number } | null {
  if (typeof v !== "string") return null;
  const m = /^(\d{1,2}):(\d{2})(?::\d{2})?$/.exec(v.trim());
  if (!m) return null;
  const hour = Number(m[1]);
  const minute = Number(m[2]);
  return hour <= 23 && minute <= 59 ? { hour, minute } : null;
}

/**
 * Classify a stored UTC cron back into a LOCAL builder schedule. Anything that
 * is not one of the shapes generateCron emits (custom steps/ranges/lists, a
 * non-`*` month, five-plus fields, unparseable ints) falls through to Advanced,
 * carrying the raw string verbatim so the user still sees exactly what runs.
 */
export function parseCron(
  cron: string | null,
  offsetMinutes: number = getOffsetMinutes(),
): Schedule {
  const raw = (cron ?? "").trim();
  if (!raw) return { mode: "off" };

  const advanced: Schedule = { mode: "advanced", cron: raw };
  const parts = raw.split(/\s+/);
  if (parts.length !== 5) return advanced;
  const [miF, hoF, domF, monF, dowF] = parts;

  // The builder never touches the month field.
  if (monF !== "*") return advanced;

  // Hourly: `M * * * *`
  if (hoF === "*" && domF === "*" && dowF === "*") {
    const mi = intField(miF, 0, 59);
    if (mi === null) return advanced;
    return { mode: "hourly", minute: mod(mi - offsetMinutes, 60) };
  }

  const mi = intField(miF, 0, 59);
  const ho = intField(hoF, 0, 23);
  if (mi === null || ho === null) return advanced;

  // Daily: `M H * * *`
  if (domF === "*" && dowF === "*") {
    const { hour, minute } = utcTimeToLocal(ho, mi, offsetMinutes);
    return { mode: "daily", hour, minute };
  }

  // Weekly: `M H * * D[,D...]`
  if (domF === "*" && dowF !== "*") {
    const rawDows: number[] = [];
    for (const tok of dowF.split(",")) {
      const d = intField(tok, 0, 7);
      if (d === null) return advanced;
      rawDows.push(d === 7 ? 0 : d); // cron allows 7 == Sunday
    }
    const { hour, minute, dayShift } = utcTimeToLocal(ho, mi, offsetMinutes);
    const local = sortedUniq(rawDows.map((d) => mod(d + dayShift, 7)));
    return { mode: "weekly", hour, minute, daysOfWeek: local };
  }

  // Monthly: `M H D * *`. A cron whose local time falls on another day is one
  // the builder refuses (Ruling 10), so it opens in Advanced, verbatim.
  if (dowF === "*" && domF !== "*") {
    const dom = intField(domF, 1, 31);
    if (dom === null) return advanced;
    const { hour, minute, dayShift } = utcTimeToLocal(ho, mi, offsetMinutes);
    if (dayShift !== 0) return advanced;
    return { mode: "monthly", hour, minute, dayOfMonth: dom };
  }

  return advanced;
}

const DOW_LABELS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const pad2 = (n: number): string => String(n).padStart(2, "0");

/** "14:00" formatter for a local hour/minute. */
export function fmtTime(hour: number, minute: number): string {
  return `${pad2(hour)}:${pad2(minute)}`;
}

/**
 * One-line, human, LOCAL-time summary of a builder schedule. Advanced echoes the
 * raw cron and is labelled UTC. Never interpolates untrusted markup — callers
 * render the returned string as text.
 */
export function describeSchedule(s: Schedule): string {
  switch (s.mode) {
    case "off":
      return "No automatic scans (manual only).";
    case "hourly":
      return `Runs every hour at :${pad2(s.minute ?? 0)} (local).`;
    case "daily":
      return `Runs daily at ${fmtTime(s.hour ?? 0, s.minute ?? 0)} (local).`;
    case "weekly": {
      const days = sortedUniq(s.daysOfWeek ?? []);
      if (days.length === 0) return "Pick at least one day.";
      const names = days.map((d) => DOW_LABELS[d] ?? "?").join(", ");
      return `Runs weekly on ${names} at ${fmtTime(s.hour ?? 0, s.minute ?? 0)} (local).`;
    }
    case "monthly":
      return `Runs monthly on day ${s.dayOfMonth ?? 1} at ${fmtTime(s.hour ?? 0, s.minute ?? 0)} (local).`;
    case "advanced": {
      const raw = (s.cron ?? "").trim();
      return raw ? `Runs on cron "${raw}" (UTC).` : "No automatic scans (manual only).";
    }
  }
}
