<script lang="ts">
  import { onMount } from "svelte";
  import {
    getAuthConfig, saveAuthConfig, testAuthProvider, fetchLdapCert, friendlyError,
    SECRET_UNCHANGED, type AuthProviderConfig, type AuthTestResult,
    type FetchCertResult,
  } from "./api";
  import { numOrNull } from "./numInput";
  import { Alert, Badge, Button, Checkbox, Field, Input, StateBlock, Textarea } from "./ui";

  // Admin > Authentication (2026-08-20): GUI configuration of AD/LDAP login, the
  // AD directory sync, and OIDC SSO — previously env-only (FILEARR_LDAP_* /
  // FILEARR_OIDC_*). A value saved here OVERRIDES the matching env default per
  // field; each field shows its source (gui | env). Secrets are write-only:
  // the server returns has_* flags, and an untouched password field sends the
  // SECRET_UNCHANGED sentinel so the stored value is kept.

  type FieldDef = {
    key: string;
    label: string;
    type: "text" | "password" | "bool" | "number" | "longtext" | "json" | "pem";
    help?: string;
    secret?: boolean; // paired with a has_<key> flag from the server
  };
  type Provider = { id: string; title: string; blurb: string; test?: string; fields: FieldDef[] };

  const PROVIDERS: Provider[] = [
    {
      id: "ldap", title: "LDAP / Active Directory login",
      blurb: "Users sign in with their directory password via a real bind. Local accounts still work; LDAP is the fall-through.",
      test: "Test bind",
      fields: [
        { key: "ldap_enabled", label: "Enable LDAP login", type: "bool" },
        { key: "ldap_server", label: "Server URL", type: "text", help: "ldaps://dc.corp.example.com (TLS-first; ldap:// on a non-loopback host needs StartTLS)" },
        { key: "ldap_start_tls", label: "StartTLS (upgrade ldap://)", type: "bool" },
        { key: "ldap_tls_verify", label: "Verify server certificate", type: "bool" },
        { key: "ldap_tls_ca_cert_file", label: "CA cert file path (optional)", type: "text", help: "For a mounted file. To paste or fetch instead, use the CA certificate (PEM) box below." },
        { key: "ldap_tls_ca_cert_pem", label: "CA certificate (PEM)", type: "pem", help: "Paste the issuing-CA chain, or click 'Fetch from server' to pull it. No file mount needed." },
        { key: "ldap_allow_plaintext", label: "Allow plaintext (discouraged)", type: "bool" },
        { key: "ldap_timeout", label: "Timeout (seconds)", type: "number" },
        { key: "ldap_bind_dn", label: "Service bind DN", type: "text", help: "cn=svc,ou=…,dc=… — used for search-then-bind and directory enumeration" },
        { key: "ldap_bind_password", label: "Service bind password", type: "password", secret: true },
        { key: "ldap_user_base", label: "User search base", type: "text" },
        { key: "ldap_user_filter", label: "User filter", type: "text", help: "AD: (sAMAccountName={username})" },
        { key: "ldap_user_dn_template", label: "User DN template (direct-bind)", type: "text", help: "uid={username},ou=people,dc=… — alternative to search-then-bind" },
        { key: "ldap_attr_username", label: "Username attribute", type: "text", help: "AD: sAMAccountName" },
        { key: "ldap_username_format", label: "Account name style", type: "text", help: "upn (eric@domain — default) · netbios (DOMAIN\\eric via msDS-PrincipalName) · attr (bare username attribute). Applied at every login; a renamed-away account heals when the styled name is free." },
        { key: "ldap_attr_upn", label: "UPN attribute", type: "text", help: "AD: userPrincipalName" },
        { key: "ldap_attr_display_name", label: "Display-name attribute", type: "text", help: "AD: displayName — synced onto the account at every login" },
        { key: "ldap_attr_email", label: "Email attribute", type: "text" },
        { key: "ldap_attr_uid", label: "Stable-id attribute", type: "text", help: "AD: objectGUID · OpenLDAP: entryUUID" },
        { key: "ldap_use_memberof", label: "Read memberOf on the user entry", type: "bool" },
        { key: "ldap_attr_memberof", label: "memberOf attribute", type: "text" },
        { key: "ldap_group_base", label: "Group search base", type: "text" },
        { key: "ldap_group_filter", label: "Group filter", type: "text", help: "AD: (member={user_dn})" },
        { key: "ldap_role_map", label: "Group → role map", type: "longtext", help: "cn=admins,…=>admin;cn=staff,…=>user  (';'-separated, '=>' delimiter)" },
        { key: "ldap_default_role", label: "Default role (empty = refuse unmapped)", type: "text" },
        { key: "ldap_auto_provision", label: "Auto-provision on first login", type: "bool" },
        { key: "ldap_group_sync", label: "Sync groups to principal groups", type: "bool" },
      ],
    },
    {
      id: "directory", title: "AD directory sync",
      blurb: "Resolves the SIDs agents push in permission snapshots into named identities + group membership. Cross-forest via the endpoints list.",
      test: "Test enumeration",
      fields: [
        { key: "ldap_directory_sync_enabled", label: "Enable directory sync", type: "bool" },
        { key: "ldap_directory_user_base", label: "User base (falls back to login base)", type: "text" },
        { key: "ldap_directory_group_base", label: "Group base (falls back to login base)", type: "text" },
        { key: "ldap_directory_user_filter", label: "User filter", type: "text" },
        { key: "ldap_directory_group_filter", label: "Group filter", type: "text" },
        { key: "ldap_directory_domain", label: "Domain (NetBIOS, e.g. CORP)", type: "text", help: "Empty = derive from each object's DN" },
        { key: "ldap_attr_object_sid", label: "objectSid attribute", type: "text" },
        { key: "ldap_attr_object_guid", label: "objectGUID attribute", type: "text" },
        { key: "ldap_attr_sam", label: "sAMAccountName attribute", type: "text" },
        { key: "ldap_attr_display_name", label: "displayName attribute", type: "text" },
        { key: "ldap_attr_upn", label: "userPrincipalName attribute", type: "text" },
        { key: "ldap_attr_member_of_dir", label: "memberOf attribute", type: "text" },
        { key: "ldap_directory_page_size", label: "Page size", type: "number" },
        { key: "ldap_directory_max_objects", label: "Max objects per sync", type: "number" },
        { key: "ldap_directories", label: "Cross-forest endpoints (JSON list)", type: "json", help: '[{"server":"ldaps://dc.acme:636","bind_dn":"…","bind_password":"…","user_base":"dc=acme,dc=com","domain":"ACME","label":"acme"}] — empty = use the single config above' },
      ],
    },
    {
      id: "oidc", title: "OIDC single sign-on",
      blurb: "Authorization-code + PKCE against any OpenID Connect provider (Entra ID, Keycloak, Authentik, Okta…).",
      test: "Fetch discovery",
      fields: [
        { key: "oidc_enabled", label: "Enable OIDC SSO", type: "bool" },
        { key: "oidc_issuer", label: "Issuer URL", type: "text", help: "https://login.microsoftonline.com/<tenant>/v2.0 — the .well-known base" },
        { key: "oidc_client_id", label: "Client ID", type: "text" },
        { key: "oidc_client_secret", label: "Client secret", type: "password", secret: true },
        { key: "oidc_scopes", label: "Scopes", type: "text", help: "must include openid" },
        { key: "oidc_redirect_uri", label: "Redirect URI (optional override)", type: "text", help: "Empty = derived from the request; must match the IdP registration" },
        { key: "oidc_role_claim", label: "Role/group claim", type: "text", help: "e.g. roles or groups" },
        { key: "oidc_role_map", label: "Claim value → role map", type: "longtext", help: "admins:admin,staff:user  (','-separated, ':' delimiter)" },
        { key: "oidc_default_role", label: "Default role (empty = refuse unmapped)", type: "text" },
        { key: "oidc_username_claim", label: "Username claim", type: "text", help: "e.g. preferred_username" },
        { key: "oidc_group_claim", label: "Group claim (for group sync)", type: "text" },
        { key: "oidc_auto_provision", label: "Auto-provision on first login", type: "bool" },
        { key: "oidc_link_by_email", label: "Link by verified email (account-takeover risk)", type: "bool" },
        { key: "oidc_login_state_ttl_minutes", label: "Login-state TTL (minutes)", type: "number" },
        { key: "oidc_http_timeout_s", label: "HTTP timeout (seconds)", type: "number" },
      ],
    },
  ];

  let open = $state<Record<string, boolean>>({});
  let cfg = $state<Record<string, AuthProviderConfig | null>>({});
  // Local edit buffer per provider (key -> value). Secrets stay empty unless typed.
  let form = $state<Record<string, Record<string, unknown>>>({});
  let busy = $state<Record<string, boolean>>({});
  let msg = $state<Record<string, string>>({});
  let testResult = $state<Record<string, AuthTestResult | null>>({});
  let fetched = $state<FetchCertResult | null>(null);
  let fetchBusy = $state(false);
  // Per-origin errors: a provider's config load (in place of its fields), its
  // Save/Test (under those buttons), and the CA fetch (under its button).
  let loadError = $state<Record<string, string>>({});
  let actionError = $state<Record<string, string>>({});
  let fetchError = $state("");

  async function fetchCert(p: Provider) {
    fetchError = ""; fetched = null; fetchBusy = true;
    try {
      fetched = await fetchLdapCert(buildBody(p));
    } catch (e) {
      fetchError = friendlyError(e);
    } finally {
      fetchBusy = false;
    }
  }

  function useFetchedCert(p: Provider) {
    if (fetched?.suggested_ca_pem) {
      form[p.id]["ldap_tls_ca_cert_pem"] = fetched.suggested_ca_pem;
      fetched = null;
    }
  }

  async function load(p: Provider) {
    loadError[p.id] = "";
    try {
      const data = await getAuthConfig(p.id);
      cfg[p.id] = data;
      const f: Record<string, unknown> = {};
      for (const fld of p.fields) {
        if (fld.secret) { f[fld.key] = ""; continue; } // never prefill a secret
        const v = data[fld.key];
        f[fld.key] = fld.type === "json" ? JSON.stringify(v ?? [], null, 2) : (v ?? "");
      }
      form[p.id] = f;
    } catch (e) {
      loadError[p.id] = friendlyError(e);
    }
  }

  async function toggle(p: Provider) {
    open[p.id] = !open[p.id];
    if (open[p.id] && !cfg[p.id]) await load(p);
  }

  function source(p: Provider, key: string): string {
    return cfg[p.id]?._source?.[key] ?? "env";
  }
  function hasSecret(p: Provider, key: string): boolean {
    return Boolean(cfg[p.id]?.[`has_${key}`]);
  }

  // Build the PUT/test body from the edit buffer, coercing types and applying
  // secret semantics (empty + already-stored => keep sentinel).
  function buildBody(p: Provider): Record<string, unknown> {
    const out: Record<string, unknown> = {};
    const f = form[p.id] ?? {};
    for (const fld of p.fields) {
      const raw = f[fld.key];
      if (fld.secret) {
        if (raw === "" || raw == null) {
          out[fld.key] = hasSecret(p, fld.key) ? SECRET_UNCHANGED : "";
        } else out[fld.key] = raw;
        continue;
      }
      if (fld.type === "bool") out[fld.key] = Boolean(raw);
      // An emptied type="number" binds null: send null (set_config pops the
      // override so the env value applies; an omitted key stays unchanged),
      // never Number(null) === 0.
      else if (fld.type === "number") out[fld.key] = numOrNull(raw);
      else if (fld.type === "json") {
        try { out[fld.key] = JSON.parse((raw as string) || "[]"); }
        catch { throw new Error(`${fld.label}: invalid JSON`); }
      } else out[fld.key] = raw;
    }
    return out;
  }

  async function save(p: Provider) {
    actionError[p.id] = ""; msg[p.id] = ""; busy[p.id] = true;
    try {
      const body = buildBody(p);
      cfg[p.id] = await saveAuthConfig(p.id, body);
      // Reset secret fields (kept server-side) and re-sync from the redacted read.
      await load(p);
      msg[p.id] = "Saved.";
    } catch (e) {
      actionError[p.id] = friendlyError(e);
    } finally {
      busy[p.id] = false;
    }
  }

  async function runTest(p: Provider) {
    actionError[p.id] = ""; msg[p.id] = ""; testResult[p.id] = null; busy[p.id] = true;
    try {
      testResult[p.id] = await testAuthProvider(p.id, buildBody(p));
    } catch (e) {
      actionError[p.id] = friendlyError(e);
    } finally {
      busy[p.id] = false;
    }
  }

  onMount(() => { /* lazy — panels load on expand */ });
</script>

{#snippet sourceBadge(p: Provider, key: string)}
  <Badge tone={source(p, key) === "gui" ? "accent" : "neutral"}>{source(p, key)}</Badge>
{/snippet}

<section class="mt-6 rounded-panel border border-border bg-surface-2 p-4">
  <h3 class="text-base font-semibold">Authentication</h3>
  <p class="mt-1 text-sm text-slate-500">
    Configure directory login and single sign-on from the console. A value set here
    overrides the matching <code>FILEARR_*</code> environment variable per field
    (the source is shown next to each). Secrets are stored encrypted and never shown.
  </p>

  {#each PROVIDERS as p (p.id)}
    <div class="mt-3 rounded-control border border-border">
      <!-- A disclosure, not an action: stays a raw button with token classes. -->
      <button
        type="button"
        class="flex w-full items-center justify-between px-4 py-3 text-left"
        aria-expanded={!!open[p.id]}
        onclick={() => toggle(p)}>
        <span>
          <span class="font-medium">{p.title}</span>
          <span class="ml-2 block text-xs text-muted sm:inline">{p.blurb}</span>
        </span>
        <span class="text-muted" aria-hidden="true">{open[p.id] ? "▲" : "▼"}</span>
      </button>

      {#if open[p.id]}
        <div class="border-t border-border px-4 py-3">
          {#if loadError[p.id]}
            <StateBlock state="error" compact message={loadError[p.id]} onRetry={() => load(p)} />
          {:else if !form[p.id]}
            <StateBlock state="loading" compact />
          {:else}
            <div class="grid grid-cols-1 gap-3 md:grid-cols-2">
              {#each p.fields as fld (fld.key)}
                <div class="flex items-start gap-2 {fld.type === 'longtext' || fld.type === 'json' || fld.type === 'pem' ? 'md:col-span-2' : ''}">
                  {#if fld.type === "bool"}
                    <Checkbox class="flex-1" label={fld.label} description={fld.help}
                      bind:checked={form[p.id][fld.key] as boolean} />
                  {:else}
                    <Field class="min-w-0 flex-1" label={fld.label} hint={fld.help}>
                      {#if fld.type === "number"}
                        <Input type="number" bind:value={form[p.id][fld.key] as number | null} />
                      {:else if fld.type === "password"}
                        <Input type="password" autocomplete="new-password"
                          placeholder={hasSecret(p, fld.key) ? "•••••• (stored — leave blank to keep)" : "not set"}
                          bind:value={form[p.id][fld.key] as string} />
                      {:else if fld.type === "pem"}
                        <Textarea rows={4} placeholder="-----BEGIN CERTIFICATE-----" class="font-mono text-xs"
                          bind:value={form[p.id][fld.key] as string} />
                      {:else if fld.type === "longtext" || fld.type === "json"}
                        <Textarea rows={fld.type === "json" ? 5 : 2} class="font-mono text-xs"
                          bind:value={form[p.id][fld.key] as string} />
                      {:else}
                        <Input type="text" bind:value={form[p.id][fld.key] as string} />
                      {/if}
                    </Field>
                  {/if}
                  {@render sourceBadge(p, fld.key)}
                </div>
                {#if fld.type === "pem"}
                  <div class="md:col-span-2">
                    <Button size="sm" disabled={fetchBusy} onclick={() => fetchCert(p)}>
                      {fetchBusy ? "fetching…" : "Fetch from server"}
                    </Button>
                    {#if fetchError}
                      <Alert tone="danger" class="mt-2" onDismiss={() => (fetchError = "")}>{fetchError}</Alert>
                    {/if}
                    {#if fetched}
                      {#if fetched.ok}
                        <Alert tone="warn" class="mt-2 text-xs"
                          title={`Verify this fingerprint out-of-band before trusting (${fetched.host}:${fetched.port})`}>
                          {#each fetched.chain ?? [] as c, i}
                            <div class="mt-1 border-t border-border pt-1">
                              <div><span class="text-muted">{i === 0 ? "leaf" : c.is_self_signed ? "root CA" : "CA"}:</span> {c.subject}
                                {#if c.via_aia}<span title="Not presented by the server — retrieved by following the certificate's Authority Information Access pointer"><Badge tone="warn" class="ml-1">via AIA</Badge></span>{/if}</div>
                              <div class="text-muted">issuer: {c.issuer}</div>
                              <div class="font-mono break-all">SHA-256: {c.sha256}</div>
                              <div class="text-muted">expires {new Date(c.not_after).toLocaleDateString()}</div>
                            </div>
                          {/each}
                          <div class="mt-1 text-fg">{fetched.note}</div>
                          <Button size="sm" variant="primary" class="mt-1" onclick={() => useFetchedCert(p)}>
                            Use this CA (fills the box — review, then Save)
                          </Button>
                        </Alert>
                      {:else}
                        <Alert tone="danger" class="mt-2 text-xs" onDismiss={() => (fetched = null)}>✗ {fetched.error}</Alert>
                      {/if}
                    {/if}
                  </div>
                {/if}
              {/each}
            </div>

            <div class="mt-3 flex flex-wrap items-center gap-2">
              <Button variant="primary" disabled={busy[p.id]} onclick={() => save(p)}>
                {busy[p.id] ? "…" : "Save"}
              </Button>
              {#if p.test}
                <Button disabled={busy[p.id]} onclick={() => runTest(p)}>{p.test}</Button>
              {/if}
              {#if msg[p.id]}<span class="text-sm text-ok" role="status">{msg[p.id]}</span>{/if}
            </div>
            {#if actionError[p.id]}
              <Alert tone="danger" class="mt-2" onDismiss={() => (actionError[p.id] = "")}>{actionError[p.id]}</Alert>
            {/if}

            {#if testResult[p.id]}
              {@const t = testResult[p.id]!}
              <Alert tone={t.ok ? "ok" : "danger"} class="mt-2" onDismiss={() => (testResult[p.id] = null)}>
                {#if t.ok}
                  {#if t.detail}<div>✓ {t.detail}</div>{/if}
                  {#if t.users != null}<div>Users: {t.users} · Groups: {t.groups}</div>{/if}
                  {#if t.issuer}<div>✓ Issuer: {t.issuer}</div><div class="text-xs opacity-80">token: {t.token_endpoint} · jwks: {t.jwks_uri}</div>{/if}
                  {#if t.endpoints}
                    {#each t.endpoints as ep}
                      <div>{ep.ok ? "✓" : "✗"} {ep.label}: {ep.ok ? `${ep.users} users, ${ep.groups} groups` : ep.error}</div>
                    {/each}
                  {/if}
                {:else}
                  ✗ {t.error}
                {/if}
              </Alert>
            {/if}
          {/if}
        </div>
      {/if}
    </div>
  {/each}
</section>
