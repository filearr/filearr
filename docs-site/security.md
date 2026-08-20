# Security features

This page describes Filearr's security surface: the auth model, RBAC, the search
tenant-scoping, the agent-plane trust model, audit logging, rate limiting, signed
agent updates, the data-safety (recycle-bin) model, and secrets posture.

## Authentication model

Filearr supports two credential carriers, side by side:

- **API keys (Bearer tokens).** Prefixed, high-entropy CSPRNG tokens, stored
  **sha256-hashed at rest** (high entropy, so no slow KDF needed). Each key has
  one or more scopes: **`read`**, **`write`**, **`admin`** (admin implies the
  others). Use them for `*arr`-style integrations and scripts:

    ```http
    Authorization: Bearer <api-key>
    ```

    Admins create and revoke them under **Admin → API keys** (name, scopes,
    optional expiry in days, and the **service account** that owns the key —
    every key belongs to one; pick an existing account or create it inline).
    **Service accounts** (Admin → Service accounts) are the non-human
    principals: one per integration, so a retired or compromised integration
    is one switch — *disable* refuses every key the account owns on the next
    request, *delete* revokes them. Keys that predate service accounts sit under
    *Pre-existing keys*. The key material is shown exactly once at creation.
    Revoking is immediate — the next request with that key gets 401.
    Every mint/revoke lands in the audit log (`API_KEY_MINTED` /
    `API_KEY_REVOKED`). The REST equivalents are `GET/POST /api/v1/api-keys`
    and `DELETE /api/v1/api-keys/{id}` (admin scope). LLM access keys are a
    separate family (Admin → LLM access keys): they carry a facade role and are
    pinned to `read` on the main API.

- **Interactive sessions.** Postgres-backed, opaque session cookies (not
  stateless JWTs — so revocation is O(1): delete the row). The cookie is
  `HttpOnly` and `SameSite` (lax by default, so OIDC callbacks work while
  state-changing requests stay CSRF-safe), and `Secure` whenever the request
  arrived over HTTPS. Lifetimes are tunable: a 30-day absolute cap, a 7-day
  idle window, and 10-minute opaque-token rotation by default.

Local passwords use **argon2id** (a slow, memory-hard KDF appropriate for
low-entropy human secrets) — a deliberately different trust model from the
sha256-at-rest API keys.

**Auth is ON by default** (`FILEARR_AUTH_ENABLED=true`): the first browser
visit shows a one-time **"create the administrator account"** screen (backed by
`POST /api/v1/auth/bootstrap`, which refuses once any user exists). Setting
`FILEARR_AUTH_ENABLED=false` opens every route — a development/trusted-LAN
convenience only. The **first admin is always a local account**, which is your
break-glass path if a federated provider locks everyone out. See
[Operations → authentication](operations.md#enabling-authentication).

### Federated login (optional)

- **OIDC SSO** — Authlib relying party (authorization-code + PKCE, JWKS ID-token
  validation). Env-configured; role mapping evaluated at every login; JIT
  provisioning and optional group sync. Email-based account linking is **off by
  default** (an account-takeover surface) and only ever links on an IdP-asserted
  `email_verified=true` exact match.
- **LDAP / Active Directory** — bind auth via ldap3, TLS-first (StartTLS upgrade
  for non-loopback `ldap://`, refused without TLS unless an operator explicitly
  allows plaintext). Direct-bind or search-then-bind, group→role mapping, and
  optional group sync.

Both fail **closed**: a half-configured provider's endpoints 404 rather than
500ing, and an unmapped user is refused when you leave the default role empty.

## AD/LDAP directory sync — attributing permissions to accounts {#directory-sync}

The permissions collector reads Windows ACLs, which name a principal by **SID**
(`S-1-5-21-…`). A domain-joined agent resolves the SID to `DOMAIN\name` where it
can, but a non-joined agent — or one that can't reach a DC — pushes the bare
SID. To turn those SIDs into named identities, central runs a **directory sync**
(this is a central-only feature; agents never talk to your DC for this):

1. **Enumeration.** With `FILEARR_LDAP_DIRECTORY_SYNC_ENABLED=true` and a service
   bind, central enumerates AD users and groups (reusing the same TLS-first
   `ldap_*` transport as login), capturing each object's `objectSid`,
   `objectGUID`, `sAMAccountName`, `displayName`, `userPrincipalName` and
   `memberOf`. Stored in `directory_objects` (the directory of record).
2. **Reconciliation.** Every SID that actually appears in a permission snapshot
   is matched against the directory and, on a hit, written to `principal_aliases`
   (tagged `source='ldap'`) as `SID → DOMAIN\name (Full Name)`. The permission
   reports already resolve through that table, so an ACE now reads
   `CORP\jsmith (John Smith)` instead of a raw SID — **no report change, and the
   stored snapshot stays verbatim** (the mapping is presentation, never a rewrite
   of evidence). A manual alias override (`source='manual'`) is never clobbered
   by a sync; a since-deleted account is kept as a tombstone so its ACLs still
   attribute to `name (deleted)`.
3. **Group expansion.** `directory_objects.member_of_sids` (resolved during the
   sync) lets `GET /permissions/effective-access?expand_groups=true` (default)
   grow a caller's identity closure by their AD group membership — nested groups
   included — so a grant to a group correctly attributes to its members.

The sync runs on a schedule (**Sync AD/LDAP directory**, default 03:40 daily,
editable on the Jobs page) and on demand (`POST /api/v1/directory/sync`).
`GET /api/v1/directory/status` reports how many snapshot SIDs resolve vs remain
unresolved (an unresolved count points at a missing base DN, a foreign domain,
or a deleted account); `GET /api/v1/directory/objects` browses the directory.
AD groups also map to Filearr roles at login via
[`FILEARR_LDAP_ROLE_MAP`](#roles), so directory membership drives both access
attribution *and* RBAC. All of it fails **closed**: disabled by default, and a
missing service bind refuses enumeration rather than pulling a partial tree.

## RBAC: groups, path grants, and ltree scoping

Beyond the coarse read/write/admin scopes, Filearr has **path-scoped RBAC**:

- **Principals** are users or service accounts; **groups** collect principals.
- A **path grant** ties a principal or group to a path scope and an action set.
  The item's scope is encoded as an `ltree` value derived from its
  `(library, relative path)`, and grants are matched by ltree ancestry — so a
  grant on a folder covers everything beneath it.
- A non-admin principal with no grants sees **nothing** (fail-closed); an admin
  bypasses scoping entirely.

!!! note "ltree columns are real extension types"
    The scope columns are backed by the Postgres `ltree` extension type (with a
    GiST ancestor index), with a text fallback where the extension is
    unavailable. This matters operationally — see the
    [ltree bind-cast class](operations.md#the-ltree-bind-cast-42804-error-class)
    in the runbook.

## Tenant-scoped search

RBAC extends into Meilisearch. A scoped (non-admin) principal's search is
constrained by a **server-side proxy filter**: the API injects the principal's
compiled scope filter into the Meilisearch query body (a principal with no grants
compiles to a filter that matches nothing). Because enforcement is server-side,
there is no client-visible tenant token to leak, and there is a configurable
ceiling on the compiled filter length — an over-large grant set is **refused**
(the admin must consolidate grants) rather than silently coarsened.

Note the Meilisearch tenant-token CVE pin discussed in
[Setup requirements](setup.md#dependency-versions).

## Agent-plane authentication

The agent plane (replication, reconcile, policy, commands) has its own auth,
selected by `FILEARR_AGENT_AUTH_MODE`:

| Mode | How identity is proven | Notes |
|---|---|---|
| `fingerprint` *(default, interim)* | The agent's bound cert fingerprint as a bearer token | The only durable per-agent secret before mTLS. **Caveat:** the fingerprint rotates on cert renewal, so a long-lived fleet must re-pin or migrate to mTLS. |
| `mtls-header` | A TLS-terminating proxy verifies the client cert and forwards the verified SAN | Identity is the SAN (`== agent_id`), which survives cert rotation — the drift caveat dies. Requires the proxy shared secret; the bearer path is refused. |
| `both` | Proxy header when present (hard-fails on a bad secret/SAN), else bearer | The zero-downtime transition mode. |

The mTLS path relies on a TLS-terminating proxy (Caddy's `agents.<domain>` site)
that verifies the client cert against the step-ca root and forwards a **verified**
identity as trusted headers, guarded by `FILEARR_PROXY_SHARED_SECRET`. The
mTLS-header modes fail **closed** when that secret is unset.

**Flip sequence (zero downtime):** set `both` → migrate each agent to the mTLS
site (the Go client presents its enrolled cert automatically) → set
`mtls-header`.

### Enrollment tokens

Enrollment tokens are **single-use** and **short-TTL** (default 60 minutes) —
the token is the single human-copy-paste weak link, so its blast-radius window is
deliberately small. The raw token is shown once and only its hash is stored.

## Audit log

Login, logout, session lifecycle, grant changes, and every agent mutation
(`agent_token_minted`, `agent_registered`, `agent_cert_bound`, `agent_revoked`,
`agent_ca_ott_minted`, …) are **always** recorded to a security-events table
(Admin → Audit, or `GET /api/v1/audit`, admin scope). Raw tokens and secrets
never appear in the log.

High-volume **read** auditing (a per-query event) is **off by default** (low
value outside multi-tenant SaaS) and toggled with `FILEARR_AUDIT_READS`. Retention
is split: noisy login-failure rows purge sooner than higher-value events.

!!! info "Download / export / verify are audited unconditionally"
    Actions that move data out of Filearr — downloads, exports, and verify — are
    recorded regardless of the read-audit toggle, so there is always a trail of
    what left the system.

## Rate limiting (brute-force lockout)

Login is protected by a **Postgres-backed** limiter (survives restarts, shared
across workers). It tracks **two independent buckets** per attempt: the submitted
username (catches a distributed brute force — many IPs, one account) and the
source IP. Either bucket crossing the threshold locks it, and the lock is checked
*before* the slow argon2 verify runs. Defaults: 3 failures / 120-second window →
300-second lock, returning `429 + Retry-After`.

### Client IPs behind a reverse proxy {#client-ips-behind-a-proxy}

The per-IP bucket, every security-audit row and every session record the
*caller's* address. Behind a reverse proxy that address is only available from
`X-Forwarded-For` — a header any client can forge against the directly
published app port — so Filearr honours it only when the request demonstrably
came through a proxy you trust:

- **The bundled Caddy sidecar** stamps `X-Filearr-Proxy-Trust` with
  `FILEARR_PROXY_SHARED_SECRET` on every proxied request. When the app and
  Caddy share that secret (the Proxmox deploy and the Unraid full tier set it
  on both), real client IPs appear with no further configuration.
- **A third-party proxy** (SWAG, NPM, Traefik, nginx): list its address(es)
  in `FILEARR_TRUSTED_PROXIES=10.0.0.5,172.18.0.0/16`. The chain is walked
  from the right, skipping trusted hops; the first untrusted hop is the
  client.
- `FILEARR_AUTH_RATELIMIT_TRUST_FORWARDED_FOR=true` is the **legacy** blunt
  switch (leftmost entry, no checks). Keep it only if you already rely on it
  and nothing but the proxy can reach the app port.

Without any of these the proxy's own address is recorded (you will see the
Caddy container IP on every login in the audit log). The per-username bucket
is unspoofable either way.

## Outbound webhooks (SSRF guard)

Alert and report-delivery webhooks are the only place Filearr makes outbound
HTTP requests to operator-supplied URLs, so they are fenced: the target host is
resolved and **every** DNS answer vetted before the request; private,
loopback, link-local (cloud metadata) and reserved addresses are refused by
default; the socket is pinned to the vetted IP (DNS-rebinding defence);
redirects are never followed; responses are size- and time-capped; and the
`generic` payload format is HMAC-signed. Reaching a LAN receiver is an explicit
choice: `FILEARR_WEBHOOK_ALLOWED_CIDRS` lists exact targets, or
`FILEARR_WEBHOOK_ALLOW_PRIVATE_CIDRS=true` admits RFC1918/ULA (loopback and
link-local stay denied). Details: [Alerts & notifications](alerts.md#channels).

## Signed agent updates

Agent self-update integrity does **not** trust central. Releases are Ed25519-signed
with a key that lives only on your signing machine (or hardware token); the public
key is pinned into each agent binary at build time; a mismatched sha256 or an
invalid signature refuses the update. Who takes a release is gated by the
`auto_update` key in a [configuration group](agents.md#two-groupings), and a
crash-looping new binary is **automatically rolled back** by boot counting. See
[Agents → self-update](agents.md#self-update-with-signed-releases).

Three complementary trust layers:

- **Pinned Ed25519 manifest signatures** — the operator authorized this exact
  update, valid even against a compromised central or CI. Binaries can pin
  **two comma-separated keys** (current + next) so key rotation rolls through
  the normal update channel instead of a fleet rebuild.
- **Sigstore build provenance** — every published image is attested keylessly
  by the release workflow (`actions/attest-build-provenance`: Fulcio-signed,
  Rekor-logged, no private key in custody). Anyone can verify a pull came from
  this repo's workflow: `gh attestation verify oci://ghcr.io/pwsh/filearr:latest
  -R pwsh/filearr`. The first-install agent binaries are baked inside the
  attested image, inheriting its provenance.
- **Unpinned builds** (installed via the central install scripts) accept
  central's dist update channel over their authenticated TLS session plus
  sha256 — the same trust their original install carried; pinned builds refuse
  unsigned bits entirely (fail-closed).

## Data-safety model (recycle bin / tombstones)

Scans **never hard-delete** (architecture invariant 4). A file gone from disk is
tombstoned `missing`; a user-deleted item becomes `trashed` and waits for a
scheduled recycle-bin purge (retention `FILEARR_RECYCLE_RETENTION_DAYS`, default
30 days). Only `active` items appear in search and browse. A `missing` item
returns to `active` automatically if a later scan sees the file again — identity
is `(library, relative path)`, so re-appearance re-attaches. See
[Operations → recycle-bin recovery](operations.md#recycle-bin-tombstone-recovery).

## Secrets management posture

- **What lives where.** Secrets (`FILEARR_SECRET_KEY`, `MEILI_MASTER_KEY`,
  `POSTGRES_PASSWORD`, `FILEARR_CA_PROVISIONER_JWK`, `FILEARR_PROXY_SHARED_SECRET`,
  the Cloudflare token) live in the container `.env` / compose `env_file` only —
  never in a committed compose file, and on Proxmox never in the (non-secret)
  `deploy.conf`.
- **Channel secrets are encrypted at rest.** Alert-channel credentials (SMTP
  password, webhook HMAC secret) are AES-GCM encrypted with an envelope key
  derived from `FILEARR_SECRET_KEY`, which is held **outside** Postgres — so a
  stolen database dump exposes no channel credentials. When the key is unset the
  alert-channels API returns 503 rather than storing plaintext.
- **Signing keys never touch the server.** The agent-release signing private key
  lives only on your signing machine; central only ever holds public/pinned
  material.
- **What is never logged.** The CA provisioner JWK, the proxy shared secret,
  raw enrollment tokens, agent enroll secrets, channel secrets, and OTTs are
  never written to logs — only *that* a key is missing/malformed, or a token's
  `jti`, is recorded.
- **`FILEARR_SECRET_KEY` is never auto-rotated** — rotating it would orphan
  already-encrypted channel secrets.

## Roles

A **role** is the coarse permission bundle attached to every user (and to
federated role mapping). Filearr ships three **builtin** roles — `admin`,
`user`, `viewer` — and lets an admin define custom ones in **Admin → Roles**.
Each role carries two things:

- **API scopes** — `read`, `write`, `admin`. The server normalises them:
  `write` implies `read`, and `admin` implies everything. A role that has the
  **admin scope bypasses path grants entirely** (the RBAC ceiling and grants
  are not consulted); this is shown as a "bypasses path grants" badge.
- **Ceiling actions** — the *maximum* set of RBAC actions (search metadata,
  download, edit, …) that a path grant may hand a member of this role. Path
  grants only ever **narrow** a user within the ceiling; they never widen it.
  A `viewer` with a "download" grant on a folder still cannot download if
  "download" is outside the viewer ceiling.

Custom roles are created from scratch or **cloned** from an existing role
(scopes and ceiling copied, then edited). Role names are stable slugs
(`[a-z0-9][a-z0-9_-]{1,31}`); the display name and description are free text.
Guardrails: builtin roles cannot be deleted, a role that is still assigned to
users cannot be deleted (reassign them first), and the builtin `admin` role can
never lose its admin scope — so you cannot lock yourself out by editing it.

The **Compare roles** view shows a matrix of every scope and action against
every role, plus who currently holds each role, so you can pick the least
privileged role that still has every ✓ a person needs.

Custom roles are valid targets for the **federated role maps** too:
`FILEARR_OIDC_ROLE_MAP=filearr-curators:curator` and
`FILEARR_LDAP_ROLE_MAP=cn=curators,…=>curator` assign them at login exactly
like the builtins. When a user matches several mapped roles the
highest-privilege one wins — a role with the admin scope first, then
write over read, then the wider ceiling, then the builtin order. A map entry
that names a role which does **not** exist (deleted, or not created yet) is
skipped with a warning in the log and never minted as an empty role; the user
falls back to the remaining matches or to the provider's default role.

!!! warning "A role change signs the user out"
    Changing a user's role revokes all of that user's sessions immediately;
    they sign back in with the new permissions. This is deliberate — a session
    never carries stale authority.

## Session timeouts

Interactive sessions have two independent limits:

- **Idle (inactivity) timeout** — a session that makes no request for this many
  hours expires. Every request extends the window.
- **Absolute lifetime (TTL)** — a hard cap measured from sign-in, regardless of
  activity.

Both are resolved with the precedence **user > global > env**:

1. **Env defaults** — `FILEARR_SESSION_INACTIVITY_HOURS` and
   `FILEARR_SESSION_TTL_HOURS` (7 days idle / 30 days absolute out of the box).
2. **Global runtime override** — **Admin → Sessions → Session timeouts
   (global)**. Set in hours (decimals allowed, within the server's min/max);
   *Reset to default* (or saving `0`) clears the override back to env. No
   restart needed.
3. **Per-user override** — **Admin → Users → …** on a user row. Blank or `0`
   clears the override so the global/env value applies again.

Idle-timeout changes take effect **live** for existing sessions (their idle
window is re-evaluated on the next request). Absolute-lifetime changes apply to
**new** sessions only — an already-issued session keeps the cap it was created
with. Every user can see the values that apply to them on the Account page.

## Your account

The **Account** page (top-right menu) is self-service for the signed-in user:

- **Profile** — display name and contact details (email, phone). Local
  accounts can also change their **username**; federated accounts get their
  identity from the provider.
- **Password** — local accounts change their password here (current password
  required). A password change **signs you out everywhere**: every other
  session is revoked, so a leaked old credential cannot keep a session alive.
- **Appearance** — theme and layout preferences are stored server-side with
  your account, so they follow you across browsers and devices instead of
  living in one browser's local storage.
- **Sessions & timeouts** — the idle and absolute timeouts in effect for you,
  labelled with where each value comes from (env default, global override, or
  a per-user override set by an admin), plus your active sessions with
  per-session revoke and *Log out everywhere*.
