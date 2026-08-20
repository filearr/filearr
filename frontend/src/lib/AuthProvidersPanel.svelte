<script lang="ts">
  import { onMount } from "svelte";
  import {
    getAuthConfig, saveAuthConfig, testAuthProvider, fetchLdapCert, friendlyError,
    SECRET_UNCHANGED, type AuthProviderConfig, type AuthTestResult,
    type FetchCertResult,
  } from "./api";

  // Admin > Authentication (2026-08-20): GUI configuration of AD/LDAP login, the
  // AD directory sync, and OIDC SSO — previously env-only (FILEARR_LDAP_* /
  // FILEARR_OIDC_*). A value saved here OVERRIDES the matching env default per
  // field; each field shows its source (gui | env). Secrets are write-only:
  // the server returns has_* flags, and an untouched password field sends the
  // SECRET_UNCHANGED sentinel so the stored value is kept.

  type Field = {
    key: string;
    label: string;
    type: "text" | "password" | "bool" | "number" | "longtext" | "json" | "pem";
    help?: string;
    secret?: boolean; // paired with a has_<key> flag from the server
  };
  type Provider = { id: string; title: string; blurb: string; test?: string; fields: Field[] };

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
  let error = $state("");

  async function fetchCert(p: Provider) {
    error = ""; fetched = null; fetchBusy = true;
    try {
      fetched = await fetchLdapCert(buildBody(p));
    } catch (e) {
      error = friendlyError(e);
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
      error = friendlyError(e);
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
      else if (fld.type === "number") out[fld.key] = raw === "" ? null : Number(raw);
      else if (fld.type === "json") {
        try { out[fld.key] = JSON.parse((raw as string) || "[]"); }
        catch { throw new Error(`${fld.label}: invalid JSON`); }
      } else out[fld.key] = raw;
    }
    return out;
  }

  async function save(p: Provider) {
    error = ""; msg[p.id] = ""; busy[p.id] = true;
    try {
      const body = buildBody(p);
      cfg[p.id] = await saveAuthConfig(p.id, body);
      // Reset secret fields (kept server-side) and re-sync from the redacted read.
      await load(p);
      msg[p.id] = "Saved.";
    } catch (e) {
      error = friendlyError(e);
    } finally {
      busy[p.id] = false;
    }
  }

  async function runTest(p: Provider) {
    error = ""; msg[p.id] = ""; testResult[p.id] = null; busy[p.id] = true;
    try {
      testResult[p.id] = await testAuthProvider(p.id, buildBody(p));
    } catch (e) {
      error = friendlyError(e);
    } finally {
      busy[p.id] = false;
    }
  }

  onMount(() => { /* lazy — panels load on expand */ });
</script>

<section class="mt-6 rounded-2xl border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900">
  <h3 class="text-base font-semibold">Authentication</h3>
  <p class="mt-1 text-sm text-slate-500">
    Configure directory login and single sign-on from the console. A value set here
    overrides the matching <code>FILEARR_*</code> environment variable per field
    (the source is shown next to each). Secrets are stored encrypted and never shown.
  </p>
  {#if error}<p class="mt-2 text-sm text-red-500">{error}</p>{/if}

  {#each PROVIDERS as p (p.id)}
    <div class="mt-3 rounded-xl border border-slate-200 dark:border-slate-800">
      <button
        class="flex w-full items-center justify-between px-4 py-3 text-left"
        onclick={() => toggle(p)}>
        <span>
          <span class="font-medium">{p.title}</span>
          <span class="ml-2 block text-xs text-slate-500 sm:inline">{p.blurb}</span>
        </span>
        <span class="text-slate-400">{open[p.id] ? "▲" : "▼"}</span>
      </button>

      {#if open[p.id] && form[p.id]}
        <div class="border-t border-slate-200 px-4 py-3 dark:border-slate-800">
          <div class="grid grid-cols-1 gap-3 md:grid-cols-2">
            {#each p.fields as fld (fld.key)}
              <label class="block text-sm {fld.type === 'longtext' || fld.type === 'json' || fld.type === 'pem' ? 'md:col-span-2' : ''}">
                <span class="flex items-center gap-2 text-slate-600 dark:text-slate-300">
                  {fld.label}
                  <span class="rounded px-1 text-[10px] {source(p, fld.key) === 'gui' ? 'bg-[var(--accent)]/15 text-[var(--accent)]' : 'bg-slate-100 text-slate-500 dark:bg-slate-800'}">
                    {source(p, fld.key)}
                  </span>
                </span>
                {#if fld.type === "bool"}
                  <input type="checkbox" class="mt-1 h-4 w-4"
                    bind:checked={form[p.id][fld.key] as boolean} />
                {:else if fld.type === "number"}
                  <input type="number" class="mt-1 w-full rounded border border-slate-300 px-2 py-1 dark:border-slate-700 dark:bg-slate-800"
                    bind:value={form[p.id][fld.key]} />
                {:else if fld.type === "password"}
                  <input type="password" autocomplete="new-password"
                    class="mt-1 w-full rounded border border-slate-300 px-2 py-1 dark:border-slate-700 dark:bg-slate-800"
                    placeholder={hasSecret(p, fld.key) ? "•••••• (stored — leave blank to keep)" : "not set"}
                    bind:value={form[p.id][fld.key]} />
                {:else if fld.type === "pem"}
                  <textarea rows="4" placeholder="-----BEGIN CERTIFICATE-----"
                    class="mt-1 w-full rounded border border-slate-300 px-2 py-1 font-mono text-xs dark:border-slate-700 dark:bg-slate-800"
                    bind:value={form[p.id][fld.key]}></textarea>
                  <button type="button"
                    class="mt-1 rounded border border-slate-300 px-2 py-0.5 text-xs disabled:opacity-50 dark:border-slate-700"
                    disabled={fetchBusy} onclick={() => fetchCert(p)}>
                    {fetchBusy ? "fetching…" : "Fetch from server"}
                  </button>
                  {#if fetched}
                    <div class="mt-2 rounded-lg border px-3 py-2 text-xs {fetched.ok ? 'border-amber-300 bg-amber-50 dark:border-amber-900 dark:bg-amber-950/40' : 'border-red-300 bg-red-50 dark:border-red-900 dark:bg-red-950/40'}">
                      {#if fetched.ok}
                        <div class="font-medium text-amber-800 dark:text-amber-200">
                          Verify this fingerprint out-of-band before trusting ({fetched.host}:{fetched.port})
                        </div>
                        {#each fetched.chain ?? [] as c, i}
                          <div class="mt-1 border-t border-amber-200/50 pt-1">
                            <div><span class="text-slate-500">{i === 0 ? "leaf" : c.is_self_signed ? "root CA" : "CA"}:</span> {c.subject}</div>
                            <div class="text-slate-500">issuer: {c.issuer}</div>
                            <div class="font-mono break-all">SHA-256: {c.sha256}</div>
                            <div class="text-slate-500">expires {new Date(c.not_after).toLocaleDateString()}</div>
                          </div>
                        {/each}
                        <div class="mt-1 text-slate-600 dark:text-slate-300">{fetched.note}</div>
                        <button type="button"
                          class="mt-1 rounded bg-[var(--accent)] px-2 py-0.5 text-xs font-medium text-white"
                          onclick={() => useFetchedCert(p)}>
                          Use this CA (fills the box — review, then Save)
                        </button>
                      {:else}
                        <span class="text-red-700 dark:text-red-300">✗ {fetched.error}</span>
                      {/if}
                    </div>
                  {/if}
                {:else if fld.type === "longtext" || fld.type === "json"}
                  <textarea rows={fld.type === "json" ? 5 : 2}
                    class="mt-1 w-full rounded border border-slate-300 px-2 py-1 font-mono text-xs dark:border-slate-700 dark:bg-slate-800"
                    bind:value={form[p.id][fld.key]}></textarea>
                {:else}
                  <input type="text"
                    class="mt-1 w-full rounded border border-slate-300 px-2 py-1 dark:border-slate-700 dark:bg-slate-800"
                    bind:value={form[p.id][fld.key]} />
                {/if}
                {#if fld.help}<span class="mt-0.5 block text-[11px] text-slate-400">{fld.help}</span>{/if}
              </label>
            {/each}
          </div>

          <div class="mt-3 flex flex-wrap items-center gap-2">
            <button
              class="rounded-lg bg-[var(--accent)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
              disabled={busy[p.id]} onclick={() => save(p)}>
              {busy[p.id] ? "…" : "Save"}
            </button>
            {#if p.test}
              <button
                class="rounded-lg border border-slate-300 px-3 py-1.5 text-sm disabled:opacity-50 dark:border-slate-700"
                disabled={busy[p.id]} onclick={() => runTest(p)}>
                {p.test}
              </button>
            {/if}
            {#if msg[p.id]}<span class="text-sm text-green-600">{msg[p.id]}</span>{/if}
          </div>

          {#if testResult[p.id]}
            {@const t = testResult[p.id]!}
            <div class="mt-2 rounded-lg border px-3 py-2 text-sm {t.ok ? 'border-green-300 bg-green-50 text-green-800 dark:border-green-900 dark:bg-green-950/40 dark:text-green-200' : 'border-red-300 bg-red-50 text-red-800 dark:border-red-900 dark:bg-red-950/40 dark:text-red-200'}">
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
            </div>
          {/if}
        </div>
      {/if}
    </div>
  {/each}
</section>
