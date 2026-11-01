<script lang="ts">
  // Self-service account page (#/account): profile, password, appearance
  // defaults (persisted server-side so they follow the person), session info.
  import { onMount } from "svelte";
  import {
    patchMyProfile,
    putMyPreferences,
    changeMyPassword,
    mySessionTimeouts,
    errorMessage,
    errorStatus,
    type AuthPrincipal,
    type MySessionTimeouts,
    type ProfilePatch,
  } from "./api";
  import { theme, applyTheme, applyServerPreferences, DEFAULT_ACCENT, type Mode } from "./theme.svelte";
  import { shareFormat, setShareFormat, detectedPlatform } from "./osFormat.svelte";
  import type { FormatPref } from "./osFormat";
  import SessionsPanel from "./SessionsPanel.svelte";
  import { Alert, Button, Field, Input, Select, StateBlock, toast } from "./ui";

  let {
    me,
    onUpdated,
    onSignedOut,
  }: { me: AuthPrincipal; onUpdated: (p: AuthPrincipal) => void; onSignedOut: () => void } = $props();

  const isLocal = $derived((me.auth_provider ?? "local") === "local");
  const providerLabel = $derived((me.auth_provider ?? "local").toUpperCase());

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
      profileMsg = {
        ok: false,
        text: errorStatus(e) === 409
          ? "That username is already taken (or cannot be changed for this account)."
          : errorMessage(e, { verb: "change this profile" }),
      };
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
  // Shown on the field they concern (the length rule wins, as before).
  const newPwProblem = $derived(
    newPw.length > 0 && newPw.length < 8 ? "New password must be at least 8 characters." : undefined,
  );
  const confirmPwProblem = $derived(
    !newPwProblem && confirmPw.length > 0 && confirmPw !== newPw ? "Passwords do not match." : undefined,
  );
  const pwReady = $derived(curPw.length > 0 && newPw.length >= 8 && confirmPw === newPw);

  async function savePassword() {
    if (!pwReady) return;
    pwBusy = true;
    pwMsg = null;
    try {
      await changeMyPassword(curPw, newPw);
      // Every session (this one included) is revoked: re-probe into the wall.
      // The toast carries the message there, since this page unmounts.
      const done = "Password changed. You have been signed out everywhere.";
      pwMsg = { ok: true, text: done };
      curPw = newPw = confirmPw = "";
      toast(done, { tone: "ok" });
      onSignedOut();
    } catch (e) {
      // 403 = wrong current password; 401 only means the session itself expired.
      pwMsg = {
        ok: false,
        text: errorStatus(e) === 403 ? "The current password is incorrect." : errorMessage(e),
      };
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
      prefMsg = { ok: false, text: errorMessage(e, { verb: "save preferences" }) };
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

  async function loadTimeouts() {
    timeoutsErr = null;
    try {
      timeouts = await mySessionTimeouts();
    } catch (e) {
      timeoutsErr = errorMessage(e);
    }
  }
  onMount(loadTimeouts);

</script>

<section class="mx-auto max-w-2xl">
  <h2 class="text-lg font-semibold">Account</h2>
  <p class="text-xs text-slate-500">
    Signed in as <span class="font-mono">{me.username}</span>
    <span class="text-slate-600 dark:text-slate-400">· {me.global_role} · {providerLabel}</span>
  </p>

  <div class="mt-4 space-y-8 text-sm">
    <!-- Profile -->
    <section>
      <h3 class="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">Profile</h3>
      <div class="flex max-w-md flex-col gap-3">
        <Field label="Username"
          hint={isLocal ? "This is your sign-in name." : `Managed by your identity provider (${providerLabel}).`}>
          <Input class="font-mono" bind:value={username} disabled={!isLocal} autocomplete="username" />
        </Field>
        <Field label="Display name">
          <Input bind:value={displayName} placeholder={me.username} autocomplete="name" />
        </Field>
        <Field label="Email">
          <Input type="email" bind:value={email} autocomplete="email" />
        </Field>
        <Field label="Phone">
          <Input type="tel" bind:value={phone} autocomplete="tel" />
        </Field>
      </div>
      <div class="mt-3 flex items-center gap-3">
        <Button variant="primary" disabled={!profileDirty || profileBusy} onclick={saveProfile}>
          {profileBusy ? "Saving…" : "Save profile"}
        </Button>
        {#if profileDirty && !profileBusy}
          <Button size="sm" variant="ghost" onclick={loadProfile}>reset</Button>
        {/if}
        {#if profileMsg?.ok}
          <span class="text-xs text-ok" role="status">{profileMsg.text}</span>
        {/if}
      </div>
      {#if profileMsg && !profileMsg.ok}
        <Alert tone="danger" class="mt-2" onDismiss={() => (profileMsg = null)}>{profileMsg.text}</Alert>
      {/if}
    </section>

    <!-- Password -->
    <section>
      <h3 class="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">Password</h3>
      {#if !isLocal}
        <p class="text-xs text-muted">
          Your password is managed by your identity provider ({providerLabel}); change it there.
        </p>
      {:else}
        <div class="flex max-w-md flex-col gap-3">
          <Field label="Current password">
            <Input type="password" bind:value={curPw} autocomplete="current-password" />
          </Field>
          <Field label="New password" error={newPwProblem}
            hint="At least 8 characters. Changing your password signs out every session, including this one.">
            <Input type="password" bind:value={newPw} autocomplete="new-password" minlength={8} />
          </Field>
          <Field label="Confirm new password" error={confirmPwProblem}>
            <Input type="password" bind:value={confirmPw} autocomplete="new-password" />
          </Field>
        </div>
        <div class="mt-3 flex items-center gap-3">
          <Button variant="primary" disabled={!pwReady || pwBusy} onclick={savePassword}>
            {pwBusy ? "Changing…" : "Change password"}
          </Button>
          {#if pwMsg?.ok}
            <span class="text-xs text-ok" role="status">{pwMsg.text}</span>
          {/if}
        </div>
        {#if pwMsg && !pwMsg.ok}
          <Alert tone="danger" class="mt-2" onDismiss={() => (pwMsg = null)}>{pwMsg.text}</Alert>
        {/if}
      {/if}
    </section>

    <!-- Appearance -->
    <section>
      <h3 class="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">Appearance defaults</h3>
      <div class="flex max-w-md flex-col gap-3">
        <Field label="Theme" class="w-48">
          <Select bind:value={mode} onchange={previewTheme}>
            <option value="system">System</option>
            <option value="light">Light</option>
            <option value="dark">Dark</option>
          </Select>
        </Field>
        <Field label="Accent colour">
          <div class="flex items-center gap-2">
            <div class="w-16">
              <Input type="color" class="h-9 cursor-pointer" bind:value={accent} oninput={previewTheme} />
            </div>
            <span class="font-mono text-xs text-muted">{accent}</span>
            <Button size="sm" variant="ghost" onclick={() => { accent = DEFAULT_ACCENT; previewTheme(); }}>default</Button>
          </div>
        </Field>
        <Field label="Paths" class="w-48">
          <Select bind:value={fmt} onchange={() => setShareFormat(fmt)}>
            <option value="auto">Auto ({platformLabel})</option>
            <option value="url">smb:// URL</option>
            <option value="unc">Windows UNC</option>
          </Select>
        </Field>
      </div>
      <p class="mt-1 text-xs text-muted">
        Changes preview immediately in this browser; Save stores them on your account so they apply wherever you sign in.
      </p>
      <div class="mt-3 flex items-center gap-3">
        <Button variant="primary" disabled={prefBusy} onclick={savePrefs}>{prefBusy ? "Saving…" : "Save preferences"}</Button>
        {#if prefMsg?.ok}
          <span class="text-xs text-ok" role="status">{prefMsg.text}</span>
        {/if}
      </div>
      {#if prefMsg && !prefMsg.ok}
        <Alert tone="danger" class="mt-2" onDismiss={() => (prefMsg = null)}>{prefMsg.text}</Alert>
      {/if}
    </section>

    <!-- Session -->
    <section>
      <h3 class="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">Session</h3>
      {#if timeoutsErr}
        <StateBlock state="error" compact message={timeoutsErr} onRetry={loadTimeouts} />
      {:else if !timeouts}
        <StateBlock state="loading" compact />
      {:else}
        <div class="grid grid-cols-1 gap-2 sm:grid-cols-[10rem_1fr]">
          <span class="text-slate-600 dark:text-slate-300">Idle timeout</span>
          <span>{hours(timeouts.inactivity_hours)}
            <span class="text-xs text-slate-600 dark:text-slate-400">— {sourceLabel(timeouts.inactivity_source)}</span></span>
          <span class="text-slate-600 dark:text-slate-300">Absolute lifetime</span>
          <span>{hours(timeouts.ttl_hours)}
            <span class="text-xs text-slate-600 dark:text-slate-400">— {sourceLabel(timeouts.ttl_source)}</span></span>
        </div>
      {/if}
    </section>
  </div>

  <!-- The user's own active sessions (moved here from the Admin page
       2026-08-19; admin-wide session controls stay on Admin). -->
  <SessionsPanel view="mine" />
</section>
