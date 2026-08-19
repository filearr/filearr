<script lang="ts">
  // Self-service account page (#/account): profile, password, appearance
  // defaults (persisted server-side so they follow the person), session info.
  import { onMount } from "svelte";
  import {
    patchMyProfile,
    putMyPreferences,
    changeMyPassword,
    mySessionTimeouts,
    ApiError,
    type AuthPrincipal,
    type MySessionTimeouts,
    type ProfilePatch,
  } from "./api";
  import { theme, applyTheme, applyServerPreferences, type Mode } from "./theme.svelte";
  import { shareFormat, setShareFormat, detectedPlatform } from "./osFormat.svelte";
  import type { FormatPref } from "./osFormat";
  import SessionsPanel from "./SessionsPanel.svelte";

  let { me, onUpdated }: { me: AuthPrincipal; onUpdated: (p: AuthPrincipal) => void } = $props();

  const isLocal = $derived((me.auth_provider ?? "local") === "local");
  const providerLabel = $derived((me.auth_provider ?? "local").toUpperCase());

  function errText(e: unknown): string {
    if (e instanceof ApiError) {
      if (e.status === 409) return "That username is already taken (or cannot be changed for this account).";
      if (e.status === 401) return "Not authorised — the current password may be wrong.";
      try {
        const j = JSON.parse(e.body);
        if (typeof j?.detail === "string") return j.detail;
      } catch { /* not JSON */ }
      return e.body || `Request failed (${e.status})`;
    }
    return String(e);
  }

  // --- Profile ---------------------------------------------------------------
  let username = $state("");
  let displayName = $state("");
  let email = $state("");
  let phone = $state("");
  let profileBusy = $state(false);
  let profileMsg = $state<{ ok: boolean; text: string } | null>(null);

  function loadProfile() {
    username = me.username;
    displayName = me.display_name ?? "";
    email = me.email ?? "";
    phone = me.phone ?? "";
  }
  $effect(() => { loadProfile(); });

  const profileDirty = $derived(
    (isLocal && username.trim() !== me.username) ||
      displayName.trim() !== (me.display_name ?? "") ||
      email.trim() !== (me.email ?? "") ||
      phone.trim() !== (me.phone ?? ""),
  );

  async function saveProfile() {
    const body: ProfilePatch = {};
    if (isLocal && username.trim() !== me.username) body.username = username.trim();
    if (displayName.trim() !== (me.display_name ?? "")) body.display_name = displayName.trim();
    if (email.trim() !== (me.email ?? "")) body.email = email.trim();
    if (phone.trim() !== (me.phone ?? "")) body.phone = phone.trim();
    if (Object.keys(body).length === 0) return;
    profileBusy = true;
    profileMsg = null;
    try {
      const p = await patchMyProfile(body);
      onUpdated(p);
      profileMsg = {
        ok: true,
        text: body.username
          ? `Profile saved. Your sign-in name is now "${p.username}" — use it next time you sign in.`
          : "Profile saved.",
      };
    } catch (e) {
      profileMsg = { ok: false, text: errText(e) };
    } finally {
      profileBusy = false;
    }
  }

  // --- Password --------------------------------------------------------------
  let curPw = $state("");
  let newPw = $state("");
  let confirmPw = $state("");
  let pwBusy = $state(false);
  let pwMsg = $state<{ ok: boolean; text: string } | null>(null);
  const pwProblem = $derived(
    newPw.length > 0 && newPw.length < 8
      ? "New password must be at least 8 characters."
      : confirmPw.length > 0 && confirmPw !== newPw
        ? "Passwords do not match."
        : null,
  );
  const pwReady = $derived(curPw.length > 0 && newPw.length >= 8 && confirmPw === newPw);

  async function savePassword() {
    if (!pwReady) return;
    pwBusy = true;
    pwMsg = null;
    try {
      await changeMyPassword(curPw, newPw);
      pwMsg = { ok: true, text: "Password changed — all sessions were signed out; sign in again." };
      curPw = newPw = confirmPw = "";
      setTimeout(() => {
        location.hash = "";
        location.reload();
      }, 1500);
    } catch (e) {
      pwMsg = { ok: false, text: errText(e) };
      pwBusy = false;
    }
  }

  // --- Appearance ------------------------------------------------------------
  let mode = $state<Mode>(theme.mode);
  let accent = $state(theme.accent);
  let fmt = $state<FormatPref>(shareFormat.pref);
  let prefBusy = $state(false);
  let prefMsg = $state<{ ok: boolean; text: string } | null>(null);
  const platformLabel =
    detectedPlatform === "windows" ? "Windows" : detectedPlatform === "mac" ? "Mac" : detectedPlatform === "linux" ? "Linux" : "other";

  function previewTheme() {
    theme.mode = mode;
    theme.accent = accent;
    applyTheme();
  }

  async function savePrefs() {
    prefBusy = true;
    prefMsg = null;
    previewTheme();
    setShareFormat(fmt);
    try {
      const next = { ...(me.preferences ?? {}), theme: { mode, accent }, share_format: fmt };
      const p = await putMyPreferences(next);
      applyServerPreferences(p.preferences);
      onUpdated(p);
      prefMsg = { ok: true, text: "Preferences saved — they will apply wherever you sign in." };
    } catch (e) {
      prefMsg = { ok: false, text: errText(e) };
    } finally {
      prefBusy = false;
    }
  }

  // --- Session ---------------------------------------------------------------
  let timeouts = $state<MySessionTimeouts | null>(null);
  let timeoutsErr = $state<string | null>(null);
  const sourceLabel = (s: string) =>
    s === "env" ? "deployment default (env)" : s === "global" ? "set by an administrator (global)" : "set for your account";
  const hours = (h: number) => (h % 24 === 0 && h >= 24 ? `${h / 24} day${h === 24 ? "" : "s"} (${h} h)` : `${h} h`);

  onMount(async () => {
    try {
      timeouts = await mySessionTimeouts();
    } catch (e) {
      timeoutsErr = errText(e);
    }
  });

</script>

<section class="mx-auto max-w-2xl">
  <h2 class="text-lg font-semibold">Account</h2>
  <p class="text-xs text-slate-500">
    Signed in as <span class="font-mono">{me.username}</span>
    <span class="text-slate-400">· {me.global_role} · {providerLabel}</span>
  </p>

  <div class="mt-4 space-y-8 text-sm">
    <!-- Profile -->
    <section>
      <h4 class="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">Profile</h4>
      <div class="grid grid-cols-1 gap-3 sm:grid-cols-[10rem_1fr] sm:items-start">
        <label class="pt-2 text-slate-600 dark:text-slate-300" for="acct-username">Username</label>
        <div>
          <input id="acct-username" class="w-full rounded-lg border border-slate-300 bg-transparent px-3 py-2 font-mono disabled:opacity-60 dark:border-slate-700"
            bind:value={username} disabled={!isLocal} autocomplete="username" />
          {#if !isLocal}
            <p class="mt-1 text-xs text-slate-500">Managed by your identity provider ({providerLabel}).</p>
          {:else}
            <p class="mt-1 text-xs text-slate-500">This is your sign-in name.</p>
          {/if}
        </div>
        <label class="pt-2 text-slate-600 dark:text-slate-300" for="acct-display">Display name</label>
        <input id="acct-display" class="rounded-lg border border-slate-300 bg-transparent px-3 py-2 dark:border-slate-700"
          bind:value={displayName} placeholder={me.username} autocomplete="name" />
        <label class="pt-2 text-slate-600 dark:text-slate-300" for="acct-email">Email</label>
        <input id="acct-email" type="email" class="rounded-lg border border-slate-300 bg-transparent px-3 py-2 dark:border-slate-700"
          bind:value={email} autocomplete="email" />
        <label class="pt-2 text-slate-600 dark:text-slate-300" for="acct-phone">Phone</label>
        <input id="acct-phone" type="tel" class="rounded-lg border border-slate-300 bg-transparent px-3 py-2 dark:border-slate-700"
          bind:value={phone} autocomplete="tel" />
      </div>
      <div class="mt-3 flex items-center gap-3">
        <button class="rounded-lg bg-[var(--accent)] px-3 py-1.5 text-sm text-white disabled:opacity-40"
          disabled={!profileDirty || profileBusy} onclick={saveProfile}>
          {profileBusy ? "Saving…" : "Save profile"}
        </button>
        {#if profileDirty && !profileBusy}
          <button class="text-xs text-slate-500 underline" onclick={loadProfile}>reset</button>
        {/if}
        {#if profileMsg}
          <span class="text-xs {profileMsg.ok ? 'text-emerald-600 dark:text-emerald-400' : 'text-red-600'}">{profileMsg.text}</span>
        {/if}
      </div>
    </section>

    <!-- Password -->
    <section>
      <h4 class="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">Password</h4>
      {#if !isLocal}
        <p class="text-xs text-slate-500">
          Your password is managed by your identity provider ({providerLabel}); change it there.
        </p>
      {:else}
        <div class="grid grid-cols-1 gap-3 sm:grid-cols-[10rem_1fr] sm:items-start">
          <label class="pt-2 text-slate-600 dark:text-slate-300" for="acct-pw-cur">Current password</label>
          <input id="acct-pw-cur" type="password" class="rounded-lg border border-slate-300 bg-transparent px-3 py-2 dark:border-slate-700"
            bind:value={curPw} autocomplete="current-password" />
          <label class="pt-2 text-slate-600 dark:text-slate-300" for="acct-pw-new">New password</label>
          <input id="acct-pw-new" type="password" class="rounded-lg border border-slate-300 bg-transparent px-3 py-2 dark:border-slate-700"
            bind:value={newPw} autocomplete="new-password" minlength="8" />
          <label class="pt-2 text-slate-600 dark:text-slate-300" for="acct-pw-confirm">Confirm new password</label>
          <input id="acct-pw-confirm" type="password" class="rounded-lg border border-slate-300 bg-transparent px-3 py-2 dark:border-slate-700"
            bind:value={confirmPw} autocomplete="new-password" />
        </div>
        <p class="mt-1 text-xs text-slate-500">
          At least 8 characters. Changing your password signs out every session, including this one.
        </p>
        {#if pwProblem}<p class="mt-1 text-xs text-amber-600 dark:text-amber-400">{pwProblem}</p>{/if}
        <div class="mt-3 flex items-center gap-3">
          <button class="rounded-lg bg-[var(--accent)] px-3 py-1.5 text-sm text-white disabled:opacity-40"
            disabled={!pwReady || pwBusy} onclick={savePassword}>
            {pwBusy ? "Changing…" : "Change password"}
          </button>
          {#if pwMsg}
            <span class="text-xs {pwMsg.ok ? 'text-emerald-600 dark:text-emerald-400' : 'text-red-600'}">{pwMsg.text}</span>
          {/if}
        </div>
      {/if}
    </section>

    <!-- Appearance -->
    <section>
      <h4 class="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">Appearance defaults</h4>
      <div class="grid grid-cols-1 gap-3 sm:grid-cols-[10rem_1fr] sm:items-center">
        <label class="text-slate-600 dark:text-slate-300" for="acct-mode">Theme</label>
        <select id="acct-mode" class="w-48 rounded-lg border border-slate-300 bg-transparent px-2 py-2 dark:border-slate-700 dark:bg-slate-800"
          bind:value={mode} onchange={previewTheme}>
          <option value="system">System</option>
          <option value="light">Light</option>
          <option value="dark">Dark</option>
        </select>
        <label class="text-slate-600 dark:text-slate-300" for="acct-accent">Accent colour</label>
        <div class="flex items-center gap-2">
          <input id="acct-accent" type="color" class="h-9 w-14 cursor-pointer rounded border border-slate-300 bg-transparent dark:border-slate-700"
            bind:value={accent} oninput={previewTheme} />
          <span class="font-mono text-xs text-slate-500">{accent}</span>
          <button class="text-xs text-slate-500 underline" onclick={() => { accent = "#6366f1"; previewTheme(); }}>default</button>
        </div>
        <label class="text-slate-600 dark:text-slate-300" for="acct-fmt">Paths</label>
        <select id="acct-fmt" class="w-48 rounded-lg border border-slate-300 bg-transparent px-2 py-2 dark:border-slate-700 dark:bg-slate-800"
          bind:value={fmt} onchange={() => setShareFormat(fmt)}>
          <option value="auto">Auto ({platformLabel})</option>
          <option value="url">smb:// URL</option>
          <option value="unc">Windows UNC</option>
        </select>
      </div>
      <p class="mt-1 text-xs text-slate-500">
        Changes preview immediately in this browser; Save stores them on your account so they apply wherever you sign in.
      </p>
      <div class="mt-3 flex items-center gap-3">
        <button class="rounded-lg bg-[var(--accent)] px-3 py-1.5 text-sm text-white disabled:opacity-40"
          disabled={prefBusy} onclick={savePrefs}>{prefBusy ? "Saving…" : "Save preferences"}</button>
        {#if prefMsg}
          <span class="text-xs {prefMsg.ok ? 'text-emerald-600 dark:text-emerald-400' : 'text-red-600'}">{prefMsg.text}</span>
        {/if}
      </div>
    </section>

    <!-- Session -->
    <section>
      <h4 class="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">Session</h4>
      {#if timeoutsErr}
        <p class="text-xs text-red-600">{timeoutsErr}</p>
      {:else if !timeouts}
        <p class="text-xs text-slate-400">Loading…</p>
      {:else}
        <div class="grid grid-cols-1 gap-2 sm:grid-cols-[10rem_1fr]">
          <span class="text-slate-600 dark:text-slate-300">Idle timeout</span>
          <span>{hours(timeouts.inactivity_hours)}
            <span class="text-xs text-slate-400">— {sourceLabel(timeouts.inactivity_source)}</span></span>
          <span class="text-slate-600 dark:text-slate-300">Absolute lifetime</span>
          <span>{hours(timeouts.ttl_hours)}
            <span class="text-xs text-slate-400">— {sourceLabel(timeouts.ttl_source)}</span></span>
        </div>
      {/if}
    </section>
  </div>

  <!-- The user's own active sessions (moved here from the Admin page
       2026-08-19; admin-wide session controls stay on Admin). -->
  <SessionsPanel view="mine" />
</section>
