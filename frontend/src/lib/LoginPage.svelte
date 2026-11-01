<script lang="ts">
  import { authLogin, authBootstrap, oidcLoginUrl, errorMessage, errorStatus, type AuthMode } from "./api";
  import * as safeStorage from "./safeStorage";
  // Direct paths, not the ./ui barrel: this page is in the entry chunk, and the
  // barrel would pull bits-ui in with it (see ui/index.ts, rule 2).
  import Button from "./ui/Button.svelte";
  import Field from "./ui/Field.svelte";
  import Input from "./ui/Input.svelte";

  // `mode` is "bootstrap" (no users yet → create the first admin) or "enabled"
  // (normal login). `onAuthed` is called after a successful login/bootstrap so
  // the shell can re-probe and drop the wall.
  let {
    mode,
    onAuthed,
    oidcEnabled = false,
  }: { mode: AuthMode; onAuthed: () => void; oidcEnabled?: boolean } = $props();

  let username = $state("");
  let password = $state("");
  let error = $state<string | null>(null);
  let warning = $state<string | null>(null);
  let busy = $state(false);

  // Surfaced when the page itself is being served over plain http — the session
  // cookie will not carry the Secure flag, so nudge the operator to the TLS port.
  const insecure = typeof location !== "undefined" && location.protocol === "http:";

  // Surfaced when an OIDC callback bounced back with ?sso_error=<reason> (the
  // server never leaks detail — this is a generic, friendly message).
  const ssoError =
    typeof location !== "undefined"
      ? new URLSearchParams(location.search).get("sso_error")
      : null;
  if (ssoError !== null) {
    // Shown once: drop the param so a reload or bookmark does not repeat it,
    // and the failed round trip's saved deep link with it.
    const params = new URLSearchParams(location.search);
    params.delete("sso_error");
    const qs = params.toString();
    history.replaceState(null, "", location.pathname + (qs ? `?${qs}` : "") + location.hash);
    safeStorage.remove("oidc.returnHash");
  }

  function startSso() {
    // The callback returns to "/"; App restores this hash once signed in.
    safeStorage.set("oidc.returnHash", location.hash);
    // A real top-level navigation (not fetch) so the IdP round-trip + the
    // Set-Cookie on the callback return actually take effect.
    window.location.assign(oidcLoginUrl("/"));
  }

  const isBootstrap = $derived(mode === "bootstrap");

  function loginError(err: unknown): string {
    // Generic on purpose: the backend raises 401/429, LDAP paths may surface 403; no account enumeration.
    const status = errorStatus(err);
    if (status === 400 || status === 401 || status === 403) return "Invalid username or password.";
    if (status === 429) return "Too many attempts. Try again in a minute.";
    return errorMessage(err);
  }

  async function submit(e: Event) {
    e.preventDefault();
    error = null;
    warning = null;
    busy = true;
    try {
      if (isBootstrap) {
        try {
          await authBootstrap(username.trim(), password);
        } catch (err) {
          if (errorStatus(err) === 409) {
            // Someone else created the admin first: re-probe out of bootstrap mode.
            error = "An administrator account already exists. Sign in instead.";
            onAuthed();
          } else {
            error = errorMessage(err);
          }
          return;
        }
        // First admin created — log straight in to establish the session.
        try {
          await authLogin(username.trim(), password);
        } catch (err) {
          // The admin exists now: re-probe so the page leaves bootstrap mode
          // (a retry must sign in, not try to create the admin again).
          error = loginError(err);
          onAuthed();
          return;
        }
      } else {
        const res = await authLogin(username.trim(), password);
        warning = res.warning;
      }
      onAuthed();
    } catch (err) {
      error = loginError(err);
    } finally {
      busy = false;
    }
  }
</script>

<!-- <main>: the login wall renders outside the app shell's main landmark. -->
<main class="mx-auto mt-24 max-w-sm rounded-panel border border-border p-6 shadow-sm">
  <h1 class="mb-1 text-2xl font-bold text-accent-text">Filearr</h1>
  <p class="mb-4 text-sm text-muted">
    {isBootstrap ? "First run — create the administrator account." : "Sign in to continue."}
  </p>

  {#if insecure}
    <div class="mb-4 rounded-control bg-warn-bg px-3 py-2 text-xs text-warn">
      You are on plain <strong>http</strong>. Credentials and the session cookie
      are not protected in transit — use the TLS endpoint
      (<span class="font-mono">https://&lt;host&gt;:8443</span>) once available.
    </div>
  {/if}

  {#if ssoError}
    <div class="mb-4 rounded-control bg-danger-bg px-3 py-2 text-xs text-danger" role="alert">
      Single sign-on did not complete. Please try again or use a local account.
    </div>
  {/if}

  {#if oidcEnabled}
    <Button class="mb-3 w-full justify-center" onclick={startSso}>Sign in with SSO</Button>
    <div class="mb-3 flex items-center gap-2 text-xs text-muted">
      <span class="h-px flex-1 bg-border"></span>
      or
      <span class="h-px flex-1 bg-border"></span>
    </div>
  {/if}

  <form onsubmit={submit} class="flex flex-col gap-3">
    <Field label="Username" required
      hint={"With directory (AD/LDAP) login, DOMAIN\\user, user@domain and the bare account name are all accepted."}>
      <Input bind:value={username} autocomplete="username" />
    </Field>
    <Field label="Password" required hint={isBootstrap ? "Minimum 8 characters." : undefined}>
      <Input
        type="password"
        bind:value={password}
        autocomplete={isBootstrap ? "new-password" : "current-password"}
        minlength={isBootstrap ? 8 : undefined} />
    </Field>

    {#if error}
      <p class="text-sm text-danger" role="alert">{error}</p>
    {/if}
    {#if warning}
      <p class="text-sm text-warn" role="status">{warning}</p>
    {/if}

    <Button class="mt-1 justify-center" type="submit" variant="primary" disabled={busy}>
      {busy ? "Please wait…" : isBootstrap ? "Create admin & sign in" : "Sign in"}
    </Button>
  </form>
</main>
