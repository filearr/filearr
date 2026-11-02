<script lang="ts">
  // UI-T11 — friendly scan-schedule builder. A pure view over lib/schedule.ts:
  // it presents LOCAL times (Off / Hourly / Daily / Weekly / Monthly) and, for
  // anything the builder can't express, an Advanced raw-cron escape hatch with
  // the existing cron help text. Storage stays a UTC cron string — the local->UTC
  // conversion (incl. day-of-week / day-of-month shifts when the day rolls over)
  // lives entirely in schedule.ts, pinned by shared/schedule-vectors.json.
  //
  // Props: `value` (current cron or null, UTC) seeded ONCE; emits the regenerated
  // cron-or-null through `onChange`. All dynamic strings render as text.
  //
  // Validity (P7, Ruling 10): the cron comes from the guarded `scheduleCron`,
  // so a choice that must not be stored (weekly with no day, an empty or
  // out-of-range number, a monthly time that would shift the day in UTC) emits
  // null, shows the reason under the controls, and reports it through
  // `onValidity` so the hosting form can block Save. Off is valid (null cron,
  // null error). Under a stored cron, "Next runs" previews three local times.
  import { untrack } from "svelte";
  import {
    describeSchedule,
    fmtTime,
    getOffsetMinutes,
    parseCron,
    parseMinuteInput,
    parseTimeInput,
    scheduleCron,
    type Schedule,
    type ScheduleIssueCode,
    type ScheduleMode,
  } from "./schedule";
  import { nextRuns } from "./scheduleNext";
  import { fmtDateTime } from "./format";
  import { HELP } from "./help";
  import Help from "./Help.svelte";
  import { Field, Input, Select } from "./ui";

  let {
    value,
    onChange,
    onValidity,
    offsetMinutes,
    tzLabel,
    dstNote = true,
    preview,
  }: {
    value: string | null;
    onChange: (cron: string | null) => void;
    /** The current field error (null when the choice may be stored, incl.
     *  Off), reported on mount and on every change. A host form blocks Save
     *  while it is non-null. */
    onValidity?: (error: string | null) => void;
    /** Minute offset for the wall-clock -> stored-cron conversion, in JS
     *  getTimezoneOffset() convention (+west). Omit for the classic
     *  browser-local -> fixed-UTC storage; pass 0 to author the cron VERBATIM
     *  in whatever clock the evaluator reads it against (a named zone, or the
     *  agent's own local time). */
    offsetMinutes?: number;
    /** Label shown beside the time inputs; defaults to the browser zone. */
    tzLabel?: string;
    /** The fixed-UTC "shifts across DST" caveat — true only when the stored
     *  cron is offset-converted (the default mode). Verbatim schedules either
     *  have no drift (DST-aware zones) or their own caveats; pass false. */
    dstNote?: boolean;
    /** The "Next runs" preview. Defaults to on only for the classic
     *  fixed-UTC storage (offsetMinutes omitted): a verbatim cron is read
     *  against a clock this page cannot evaluate. */
    preview?: boolean;
  } = $props();

  const errorId = $props.id();

  // Offset + initial classification are captured once; the modal/form remounts
  // this component per edit target, so seeding from the prop is intentional.
  const offset = untrack(() => offsetMinutes ?? getOffsetMinutes());
  const init = untrack(() => parseCron(value, offset));

  const MODES: { id: ScheduleMode; label: string }[] = [
    { id: "off", label: "Off" },
    { id: "hourly", label: "Hourly" },
    { id: "daily", label: "Daily" },
    { id: "weekly", label: "Weekly" },
    { id: "monthly", label: "Monthly" },
    { id: "advanced", label: "Advanced (cron)" },
  ];
  const DOW = [
    { d: 0, label: "Sun" },
    { d: 1, label: "Mon" },
    { d: 2, label: "Tue" },
    { d: 3, label: "Wed" },
    { d: 4, label: "Thu" },
    { d: 5, label: "Fri" },
    { d: 6, label: "Sat" },
  ];

  const pad2 = (n: number) => String(n).padStart(2, "0");

  let mode = $state<ScheduleMode>(init.mode);
  // Hourly minute-past-the-hour; null when the number input is emptied.
  let hourlyMinute = $state<number | null>(init.mode === "hourly" ? (init.minute ?? 0) : 0);
  // Shared local time-of-day for daily/weekly/monthly, as a type="time" Input.
  let timeStr = $state(
    init.hour != null ? `${pad2(init.hour)}:${pad2(init.minute ?? 0)}` : "03:00",
  );
  let daysOfWeek = $state<number[]>(
    init.mode === "weekly" ? [...(init.daysOfWeek ?? [])] : [1],
  );
  let dayOfMonth = $state(init.mode === "monthly" ? (init.dayOfMonth ?? 1) : 1);
  let advancedCron = $state(
    untrack(() => (init.mode === "advanced" ? (init.cron ?? "") : (value ?? ""))),
  );

  // tzLabel is reactive (the caller may re-pick the evaluation clock while
  // this component stays mounted); the browser-zone fallback is not.
  const browserZone = (() => {
    try {
      return Intl.DateTimeFormat().resolvedOptions().timeZone || "local time";
    } catch {
      return "local time";
    }
  })();
  const tzName = $derived(tzLabel ?? browserZone);

  // Empty or invalid inputs stay null so scheduleCron reports them (B32);
  // the minute is rounded and clamped, never silently 0.
  const time = $derived(parseTimeInput(timeStr));
  const hour = $derived(time?.hour ?? null);
  const minute = $derived(time?.minute ?? null);

  // The builder's current LOCAL schedule, recomputed reactively from the inputs.
  const schedule = $derived.by<Schedule>(() => {
    switch (mode) {
      case "off":
        return { mode: "off" };
      case "hourly":
        return { mode: "hourly", minute: parseMinuteInput(hourlyMinute) };
      case "daily":
        return { mode: "daily", hour, minute };
      case "weekly":
        return { mode: "weekly", hour, minute, daysOfWeek };
      case "monthly":
        return { mode: "monthly", hour, minute, dayOfMonth };
      case "advanced":
        return { mode: "advanced", cron: advancedCron };
    }
  });

  const built = $derived(scheduleCron(schedule, offset));
  const cron = $derived(built.cron);
  const issue = $derived(built.issue);
  const summary = $derived(describeSchedule(schedule));

  // Which control an issue belongs to, for its invalid styling.
  const TIME_ISSUES: ScheduleIssueCode[] = ["time-required", "time-range", "monthly-day-shift"];
  const minuteInvalid = $derived(issue?.code === "minute-required" || issue?.code === "minute-range");
  const timeInvalid = $derived(issue !== null && TIME_ISSUES.includes(issue.code));
  const describedBy = $derived(issue ? errorId : undefined);

  const showPreview = $derived(preview ?? offsetMinutes === undefined);
  // `now` is read when the cron changes; the preview is a hint, not a clock.
  const upcoming = $derived(showPreview && cron ? nextRuns(cron, 3, new Date()) : []);
  // The DST caveat only matters for wall-clock-anchored modes, and only when
  // the stored cron is a fixed-UTC conversion (dstNote prop).
  const showTzNote = $derived(
    dstNote && (mode === "daily" || mode === "weekly" || mode === "monthly"),
  );

  // Emit on every change, but not on the initial mount (parent already holds the
  // equivalent value). A fresh generate can canonicalise a hand-written cron; we
  // let that flow through only after a real interaction.
  let mounted = false;
  $effect(() => {
    const next = cron; // track
    if (!mounted) {
      mounted = true;
      return;
    }
    untrack(() => onChange(next));
  });

  // Validity is reported on mount too, so a host never starts out of sync.
  $effect(() => {
    const err = issue?.message ?? null; // track
    untrack(() => onValidity?.(err));
  });

  // Show the rounded/clamped minute once the user leaves the input.
  function normaliseMinute() {
    const n = parseMinuteInput(hourlyMinute);
    if (n !== null) hourlyMinute = n;
  }

  function toggleDay(d: number) {
    daysOfWeek = daysOfWeek.includes(d)
      ? daysOfWeek.filter((x) => x !== d)
      : [...daysOfWeek, d];
  }
</script>

<!-- Compact row layout: the mode and weekday toggles stay raw segment buttons
     (aria-pressed), and the inline controls are named by aria-label because
     the sentence around them ("At minute … past every hour") leaves no room
     for a visible Field label. -->
<div class="flex flex-col gap-2">
  <div class="flex flex-wrap gap-1">
    {#each MODES as m}
      <button
        type="button"
        aria-pressed={mode === m.id}
        class="rounded-full border px-3 py-1 text-xs {mode === m.id
          ? 'border-transparent bg-accent text-accent-fg'
          : 'border-border-strong hover:bg-surface-3'}"
        onclick={() => (mode = m.id)}>{m.label}</button>
    {/each}
  </div>

  {#if mode === "hourly"}
    <div class="flex items-center gap-2 text-xs text-muted">
      At minute
      <span class="w-20">
        <Input
          size="sm"
          type="number"
          min="0"
          max="59"
          step="1"
          aria-label="Minute past every hour"
          invalid={minuteInvalid}
          aria-describedby={describedBy}
          onblur={normaliseMinute}
          bind:value={hourlyMinute} />
      </span>
      past every hour
    </div>
  {:else if mode === "daily"}
    <div class="flex items-center gap-2 text-xs text-muted">
      At
      <span class="w-32">
        <Input
          size="sm"
          type="time"
          aria-label="Time of day ({tzName})"
          invalid={timeInvalid}
          aria-describedby={describedBy}
          bind:value={timeStr} />
      </span>
      ({tzName})
    </div>
  {:else if mode === "weekly"}
    <div class="flex flex-wrap items-center gap-1" role="group" aria-label="Days of the week" aria-describedby={describedBy}>
      {#each DOW as day}
        <button
          type="button"
          aria-pressed={daysOfWeek.includes(day.d)}
          class="rounded-full border px-2 py-1 text-xs {daysOfWeek.includes(day.d)
            ? 'border-transparent bg-accent text-accent-fg'
            : 'border-border-strong hover:bg-surface-3'}"
          onclick={() => toggleDay(day.d)}>{day.label}</button>
      {/each}
    </div>
    <div class="flex items-center gap-2 text-xs text-muted">
      At
      <span class="w-32">
        <Input
          size="sm"
          type="time"
          aria-label="Time of day ({tzName})"
          invalid={timeInvalid}
          aria-describedby={describedBy}
          bind:value={timeStr} />
      </span>
      ({tzName})
    </div>
  {:else if mode === "monthly"}
    <div class="flex items-center gap-2 text-xs text-muted">
      On day
      <span class="w-20">
        <Select size="sm" aria-label="Day of the month" bind:value={dayOfMonth}>
          {#each Array.from({ length: 31 }, (_, i) => i + 1) as d}
            <option value={d}>{d}</option>
          {/each}
        </Select>
      </span>
      at
      <span class="w-32">
        <Input
          size="sm"
          type="time"
          aria-label="Time of day ({tzName})"
          invalid={timeInvalid}
          aria-describedby={describedBy}
          bind:value={timeStr} />
      </span>
      ({tzName})
    </div>
    <p class="text-xs text-muted">
      Day 29-31 is skipped in months without that day.
    </p>
  {:else if mode === "advanced"}
    <div class="flex items-start gap-2">
      <Field label="Cron expression" class="w-64">
        <Input
          size="sm"
          class="font-mono"
          placeholder={offset === 0 ? "e.g. 0 4 * * *" : "e.g. 0 4 * * * (UTC)"}
          invalid={issue?.code === "cron-required"}
          aria-describedby={describedBy}
          bind:value={advancedCron} />
      </Field>
      <span class="pt-0.5"><Help text={HELP.scan_cron} label="scan cron" /></span>
    </div>
  {/if}

  {#if issue}
    <p id={errorId} class="text-xs text-danger" role="alert">{issue.message}</p>
  {:else}
    <p class="text-xs text-muted">{summary}</p>
    {#if showTzNote && time}
      <p class="text-xs text-muted">
        Runs at {fmtTime(time.hour, time.minute)} {tzName} (stored as UTC; shifts ±1h across DST).
      </p>
    {/if}
    {#if showPreview && cron}
      <p class="text-xs text-muted">
        {#if upcoming.length > 0}
          Next runs: {upcoming.map((d) => fmtDateTime(d)).join(" · ")}
        {:else}
          No upcoming runs for this expression.
        {/if}
      </p>
    {/if}
  {/if}
</div>
