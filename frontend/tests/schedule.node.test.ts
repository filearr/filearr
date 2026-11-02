// schedule.ts — the LOCAL builder <-> stored UTC cron layer behind
// ScheduleField. The language-neutral contract lives in
// shared/schedule-vectors.json (the backend checks the same file against
// cronsim/cron_is_due in tests/test_schedule_vectors_uit11.py); this file holds
// the TS side to it, then pins the P7 guards: weekly with no day (B18), monthly
// midnight crossing refused (B19, Ruling 10) and numeric input handling (B32).

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import {
  generateCron,
  monthlyWindow,
  parseCron,
  parseMinuteInput,
  parseTimeInput,
  scheduleCron,
  scheduleIssue,
  type Schedule,
} from "../src/lib/schedule.ts";

interface Vector {
  name: string;
  offset_minutes: number;
  builder: Schedule;
  cron: string | null;
  /** Set when the builder must refuse this choice (Ruling 10). */
  refused?: string;
  due: { utc: string; due: boolean }[];
}

const doc = JSON.parse(
  readFileSync(new URL("../../shared/schedule-vectors.json", import.meta.url), "utf8"),
) as { vectors: Vector[] };
const VECTORS = doc.vectors;

test("vectors: the shared file is present and broad", () => {
  assert.ok(VECTORS.length >= 15);
});

for (const v of VECTORS) {
  test(`vector ${v.name}: generateCron matches the shared cron`, () => {
    assert.equal(generateCron(v.builder, v.offset_minutes), v.cron);
  });

  test(`vector ${v.name}: generate(parse(cron)) round-trips`, () => {
    assert.equal(generateCron(parseCron(v.cron, v.offset_minutes), v.offset_minutes), v.cron);
  });

  test(`vector ${v.name}: guarded builder ${v.refused ? "refuses" : "accepts"}`, () => {
    const out = scheduleCron(v.builder, v.offset_minutes);
    if (v.refused) {
      assert.equal(out.cron, null);
      assert.equal(out.issue?.code, v.refused);
    } else if (v.cron === null) {
      assert.equal(out.cron, null); // "off" is not an error
      assert.equal(out.issue, null);
    } else {
      assert.equal(out.issue, null);
      assert.equal(out.cron, v.cron);
    }
  });

  // A builder-expressible vector parses back to the same LOCAL choice; a
  // refused one parses to Advanced so the stored cron is shown verbatim.
  if (v.builder.mode !== "off" && v.builder.mode !== "advanced") {
    test(`vector ${v.name}: parseCron recovers the builder choice`, () => {
      const back = parseCron(v.cron, v.offset_minutes);
      if (v.refused) {
        assert.deepEqual(back, { mode: "advanced", cron: v.cron });
      } else {
        assert.deepEqual(back, v.builder);
      }
    });
  }
}

test("vectors: refused monthly vectors are exactly the day-shifting ones", () => {
  const refused = VECTORS.filter((v) => v.refused).map((v) => v.name);
  assert.ok(refused.includes("monthly_east_day1_wrap"));
  assert.ok(refused.includes("monthly_west_day31_wrap"));
  assert.ok(!refused.includes("monthly_utc_15"));
});

// ---------------------------------------------------------------- round-trip

const OFFSETS = [0, 300, -540, -330, 330, -120, 600, -840, 720, -345];

test("round-trip: parseCron(generateCron(x)) == x across modes and offsets", () => {
  for (const off of OFFSETS) {
    const cases: Schedule[] = [
      { mode: "hourly", minute: 0 },
      { mode: "hourly", minute: 59 },
      { mode: "daily", hour: 0, minute: 0 },
      { mode: "daily", hour: 23, minute: 59 },
      { mode: "daily", hour: 12, minute: 15 },
      { mode: "weekly", hour: 0, minute: 30, daysOfWeek: [0] },
      { mode: "weekly", hour: 23, minute: 45, daysOfWeek: [0, 6] },
      { mode: "weekly", hour: 6, minute: 0, daysOfWeek: [1, 2, 3, 4, 5] },
      { mode: "monthly", hour: 12, minute: 0, dayOfMonth: 1 },
      { mode: "monthly", hour: 12, minute: 0, dayOfMonth: 31 },
    ];
    for (const s of cases) {
      const out = scheduleCron(s, off);
      if (out.issue) continue; // refused monthly: covered below
      assert.ok(out.cron, `${JSON.stringify(s)} @${off}`);
      assert.deepEqual(parseCron(out.cron, off), s, `${JSON.stringify(s)} @${off}`);
    }
  }
});

// ------------------------------------------------- weekly midnight crossings

test("weekly: late local Saturday west of UTC wraps to UTC Sunday", () => {
  // 23:00 Sat in UTC-5 is 04:00 Sun UTC.
  const s: Schedule = { mode: "weekly", hour: 23, minute: 0, daysOfWeek: [6] };
  assert.equal(scheduleCron(s, 300).cron, "0 4 * * 0");
  assert.deepEqual(parseCron("0 4 * * 0", 300), s);
});

test("weekly: early local Sunday east of UTC wraps to UTC Saturday", () => {
  // 01:00 Sun in UTC+9 is 16:00 Sat UTC.
  const s: Schedule = { mode: "weekly", hour: 1, minute: 0, daysOfWeek: [0] };
  assert.equal(scheduleCron(s, -540).cron, "0 16 * * 6");
  assert.deepEqual(parseCron("0 16 * * 6", -540), s);
});

test("weekly: cron 7 parses as Sunday", () => {
  assert.deepEqual(parseCron("0 9 * * 7", 0), {
    mode: "weekly",
    hour: 9,
    minute: 0,
    daysOfWeek: [0],
  });
});

// -------------------------------------------------------- 30-minute offsets

test("UTC+5:30: hourly :00 is stored as :30", () => {
  assert.equal(scheduleCron({ mode: "hourly", minute: 0 }, -330).cron, "30 * * * *");
  assert.deepEqual(parseCron("30 * * * *", -330), { mode: "hourly", minute: 0 });
});

test("UTC+5:30: daily 05:00 local is 23:30 UTC (previous day, daily is fine)", () => {
  assert.equal(scheduleCron({ mode: "daily", hour: 5, minute: 0 }, -330).cron, "30 23 * * *");
});

test("UTC+5:30: weekly 05:15 Mon local is 23:45 Sun UTC", () => {
  const s: Schedule = { mode: "weekly", hour: 5, minute: 15, daysOfWeek: [1] };
  assert.equal(scheduleCron(s, -330).cron, "45 23 * * 0");
  assert.deepEqual(parseCron("45 23 * * 0", -330), s);
});

test("UTC+5:30: monthly 05:29 refused, 05:30 accepted", () => {
  const early = scheduleCron({ mode: "monthly", hour: 5, minute: 29, dayOfMonth: 10 }, -330);
  assert.equal(early.issue?.code, "monthly-day-shift");
  const ok = scheduleCron({ mode: "monthly", hour: 5, minute: 30, dayOfMonth: 10 }, -330);
  assert.equal(ok.issue, null);
  assert.equal(ok.cron, "0 0 10 * *");
});

// ------------------------------------------------------------ B18: weekly

test("B18: weekly with no day selected is an error, not 'off'", () => {
  const s: Schedule = { mode: "weekly", hour: 3, minute: 0, daysOfWeek: [] };
  const out = scheduleCron(s, 0);
  assert.equal(out.cron, null);
  assert.equal(out.issue?.code, "no-days");
  assert.match(out.issue!.message, /day/i);
});

test("B18: off is never an issue", () => {
  assert.deepEqual(scheduleCron({ mode: "off" }, 0), { cron: null, issue: null });
});

test("B18: blank Advanced is an error (pick Off for no schedule)", () => {
  const out = scheduleCron({ mode: "advanced", cron: "   " }, 0);
  assert.equal(out.cron, null);
  assert.equal(out.issue?.code, "cron-required");
});

// ----------------------------------------------------------- B19: monthly

test("B19: monthly 01:00 in UTC+2 is refused, never stored as day 31", () => {
  const s: Schedule = { mode: "monthly", hour: 1, minute: 0, dayOfMonth: 1 };
  assert.equal(generateCron(s, -120), "0 23 31 * *"); // the raw shift we refuse
  const out = scheduleCron(s, -120);
  assert.equal(out.cron, null);
  assert.equal(out.issue?.code, "monthly-day-shift");
  assert.match(out.issue!.message, /Advanced/);
  assert.match(out.issue!.message, /02:00/); // earliest same-day local time
});

test("B19: monthly day 31 at 23:30 in UTC-5 is refused, never stored as day 1", () => {
  const s: Schedule = { mode: "monthly", hour: 23, minute: 30, dayOfMonth: 31 };
  assert.equal(generateCron(s, 300), "30 4 1 * *");
  const out = scheduleCron(s, 300);
  assert.equal(out.cron, null);
  assert.equal(out.issue?.code, "monthly-day-shift");
  assert.match(out.issue!.message, /18:59/); // latest same-day local time
});

test("B19: the same-day window edges are exact", () => {
  assert.deepEqual(monthlyWindow(-120), { from: "02:00", to: "23:59" });
  assert.deepEqual(monthlyWindow(300), { from: "00:00", to: "18:59" });
  assert.deepEqual(monthlyWindow(0), { from: "00:00", to: "23:59" });
  assert.deepEqual(monthlyWindow(-330), { from: "05:30", to: "23:59" });
  // UTC+2: 02:00 is 00:00 UTC same day; 01:59 is the previous UTC day.
  assert.equal(scheduleCron({ mode: "monthly", hour: 2, minute: 0, dayOfMonth: 1 }, -120).cron, "0 0 1 * *");
  assert.equal(scheduleCron({ mode: "monthly", hour: 1, minute: 59, dayOfMonth: 1 }, -120).cron, null);
  // UTC-5: 18:59 is 23:59 UTC same day; 19:00 is the next UTC day.
  assert.equal(scheduleCron({ mode: "monthly", hour: 18, minute: 59, dayOfMonth: 31 }, 300).cron, "59 23 31 * *");
  assert.equal(scheduleCron({ mode: "monthly", hour: 19, minute: 0, dayOfMonth: 31 }, 300).cron, null);
});

test("B19: a stored day-shifted monthly cron opens in Advanced, verbatim", () => {
  assert.deepEqual(parseCron("0 23 31 * *", -120), { mode: "advanced", cron: "0 23 31 * *" });
  // Same cron at UTC is a plain monthly choice.
  assert.deepEqual(parseCron("0 23 31 * *", 0), { mode: "monthly", hour: 23, minute: 0, dayOfMonth: 31 });
});

test("B19: daily and weekly crossings are NOT refused (no day-of-month)", () => {
  assert.equal(scheduleIssue({ mode: "daily", hour: 1, minute: 0 }, -120), null);
  assert.equal(scheduleIssue({ mode: "weekly", hour: 1, minute: 0, daysOfWeek: [1] }, -120), null);
});

// ------------------------------------------------- B32: numeric inputs

test("B32: parseMinuteInput rounds and clamps to 0-59", () => {
  assert.equal(parseMinuteInput(1.5), 2);
  assert.equal(parseMinuteInput(1.4), 1);
  assert.equal(parseMinuteInput(75), 59);
  assert.equal(parseMinuteInput(-3), 0);
  assert.equal(parseMinuteInput(59.6), 59);
  assert.equal(parseMinuteInput("15"), 15);
  assert.equal(parseMinuteInput(0), 0);
});

test("B32: parseMinuteInput is null for blank / non-numeric (no silent 0)", () => {
  assert.equal(parseMinuteInput(null), null);
  assert.equal(parseMinuteInput(undefined), null);
  assert.equal(parseMinuteInput(""), null);
  assert.equal(parseMinuteInput("abc"), null);
  assert.equal(parseMinuteInput(Number.NaN), null);
});

test("B32: parseTimeInput accepts HH:MM, rejects blank and out of range", () => {
  assert.deepEqual(parseTimeInput("03:05"), { hour: 3, minute: 5 });
  assert.deepEqual(parseTimeInput("3:05"), { hour: 3, minute: 5 });
  assert.deepEqual(parseTimeInput(" 23:59 "), { hour: 23, minute: 59 });
  assert.deepEqual(parseTimeInput("14:30:00"), { hour: 14, minute: 30 }); // step=1 inputs
  assert.equal(parseTimeInput(""), null);
  assert.equal(parseTimeInput(null), null);
  assert.equal(parseTimeInput("24:00"), null);
  assert.equal(parseTimeInput("12:60"), null);
  assert.equal(parseTimeInput("noon"), null);
});

test("B32: a missing minute or time is an explicit issue", () => {
  assert.equal(scheduleIssue({ mode: "hourly", minute: null }, 0)?.code, "minute-required");
  assert.equal(scheduleIssue({ mode: "daily", hour: null, minute: null }, 0)?.code, "time-required");
  assert.equal(scheduleIssue({ mode: "weekly", hour: null, minute: null, daysOfWeek: [1] }, 0)?.code, "time-required");
  assert.equal(scheduleIssue({ mode: "monthly", hour: null, minute: null, dayOfMonth: 1 }, 0)?.code, "time-required");
  assert.equal(scheduleCron({ mode: "hourly", minute: null }, 0).cron, null);
});

test("B32: a non-integer or out-of-range value never reaches the cron", () => {
  assert.equal(scheduleIssue({ mode: "hourly", minute: 1.5 }, 0)?.code, "minute-range");
  assert.equal(scheduleIssue({ mode: "hourly", minute: 60 }, 0)?.code, "minute-range");
  assert.equal(scheduleIssue({ mode: "daily", hour: 24, minute: 0 }, 0)?.code, "time-range");
  assert.equal(scheduleIssue({ mode: "monthly", hour: 3, minute: 0, dayOfMonth: 32 }, 0)?.code, "day-range");
});
