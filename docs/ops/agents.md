# Distributed agents — enrollment runbook (Phase 5, P5-T1)

Filearr's distributed-agent architecture (roadmap §1, v3) lets remote machines
run a local scanner + offline index and replicate their catalog to central. It
is **opt-in and off by default** — a single-node deploy is entirely unaffected
and never starts the CA.

**P5-T1 delivers the central-side trust root only:** the `agents` /
`enrollment_tokens` tables, the token-mint + register-first enrollment API, and
the Admin → Agents console panel. The Go agent binary, actual cert issuance, and
replication are later tasks (P5-T2..T8). This runbook covers standing up the CA
and minting an enrollment token; it flags what is not yet wired.

## 1. Turn the feature on

> **Proxmox deploys: this is now automated.** `proxmox/deploy-proxmox.sh`
> prompts once ("Enable distributed agents?"; persisted as `AGENTS_ENABLED` /
> `AGENTS_CA_URL` in `deploy.conf`) and then, on every deploy: starts step-ca
> (compose `agents` profile, either TLS mode), writes
> `FILEARR_AGENTS_ENABLED/CA_URL/CA_PROVISIONER` into the CT `.env`, pins the
> CA root fingerprint (`FILEARR_CA_FINGERPRINT`), patches the provisioner
> claims (§7.1, once), and extracts the provisioner private JWK (§7.2) into
> `FILEARR_CA_PROVISIONER_JWK` — the JWK is a SECRET and lands in the CT
> `.env` only, never in `deploy.conf`, never echoed. A post-deploy summary
> prints the enroll endpoints + the per-device commands. The sections below
> remain the manual/reference path for non-Proxmox deployments and recovery.

```bash
# .env
FILEARR_AGENTS_ENABLED=true
FILEARR_ENROLLMENT_TOKEN_TTL_MINUTES=60      # minutes-to-hours, NOT days
FILEARR_CA_URL=https://ca.filearr.lan:9000   # your step-ca (see §2)
FILEARR_CA_FINGERPRINT=<root-fingerprint>    # public pin, printed on CA init
FILEARR_CA_PROVISIONER=filearr-agents
FILEARR_AGENT_CERT_TTL_HOURS=48              # 24–72h band (short blast radius)
# P5-T2: the provisioner's DECRYPTED private JWK (JSON, one line, EC P-256/ES256).
# SECRET — treat like FILEARR_SECRET_KEY; never commit it. When UNSET the register
# response's `ca_ott` is null (agents enroll but cannot fetch certs — see §7).
FILEARR_CA_PROVISIONER_JWK='{"kty":"EC","crv":"P-256","kid":"...","x":"...","y":"...","d":"..."}'
FILEARR_CA_OTT_TTL_SECONDS=300               # OTT lifetime (short, single-use)
```

With `FILEARR_AGENTS_ENABLED=false` (default) the `/api/v1/agents` surface
returns 404 and the Admin → Agents panel stays hidden. The tables still exist
(empty) so enabling later needs **no migration**.

## 2. Stand up step-ca (optional compose profile)

step-ca (smallstep, Apache-2.0) runs **only** under the `agents` compose
profile, never in the default stack:

```bash
# .env (CA init — consumed once on first boot)
STEPCA_NAME="Filearr Agents CA"
STEPCA_DNS=ca.filearr.lan,step-ca,localhost

docker compose --profile agents up -d step-ca
docker compose logs step-ca | grep -i fingerprint   # -> FILEARR_CA_FINGERPRINT
```

The image auto-initialises a fresh CA on first boot and prints the **root
fingerprint** (public pinning material — not a secret). Copy it into
`FILEARR_CA_FINGERPRINT`. Pin: `smallstep/step-ca:0.30.2` (the version the
phase-5 research names). **On any bump, re-verify the exact patch AND the full
smallstep CVE list** — same discipline as the Meili/Caddy pins.

> **OpenBao PKI** is the documented drop-in alternative (MPL-2.0) for operators
> who already centralise PKI in Vault/OpenBao — see
> `docs/research/phase-5-distributed-agents.md` §1.2. step-ca is the default.

## 3. Mint an enrollment token and enroll a machine

**Host requirements (roadmap §20).** The agent binary is static and fully
self-contained — image, audio-cover and STL thumbnails need nothing installed.
**Video poster-frame thumbnails additionally need an `ffmpeg` binary** on the
agent host, resolved from `PATH` or the `FILEARR_AGENT_FFMPEG_PATH` override:

- Windows: `winget install ffmpeg` (or chocolatey / a gyan.dev static build)
- Debian/Ubuntu: `apt install ffmpeg` · Fedora: `dnf install ffmpeg-free`
- macOS: `brew install ffmpeg`

Without it the agent still runs — video thumbs are simply skipped (logged once).
`filearr-agent install` warns when ffmpeg is missing, and every command poll
advertises `capabilities.ffmpeg` so the fleet console can show which agents
lack it.

Admin → Agents → **Mint token** (or `POST /api/v1/agents/enrollment-tokens`,
admin scope). The raw token is shown **once** and never stored — only its
sha256 is persisted. Hand it to the new machine out-of-band (copy/paste into the
agent installer, or a QR/deep-link).

**Zero-console Windows lifecycle (2026-08-07, unified 2026-08-08):**
`scripts/manage-windows-agent.ps1` — ONE script, served pre-configured by
central at `…/api/v1/agent-dist/manage-windows-agent.ps1` (URL baked via
`filearr.urls.public_base_url`; the repo copy takes `-CentralUrl`). It
auto-detects: no installed agent → PROVISION (mints the token via the API
[401/403 mapped to a pass-`-ApiKey` hint], verified download, service
install); installed → UPDATE (manifest compare, verified swap under a
stopped service, `.old` rollback copy) + RECONFIGURE. `-ScanRoot`
(repeatable, merged into scan.json) and `-MtlsUrl` (sidecar central_url
switch, §tls.md runbook) work on both paths. The operator-driven complement
to §8 for key-pinned or self-update-disabled machines. User docs:
docs-site/agents.md §"One-script Windows lifecycle".

The enrollment handshake (R3 — **register precedes cert**):

1. **Agent → `POST /api/v1/agents/register`** `{token, hostname, platform,
   name?}` — the server validates + **consumes** the token (single-use), assigns
   the authoritative `agent_id`, and returns it plus CA bootstrap info
   (`ca.url` / `ca.fingerprint` / `ca.provisioner`) and a one-time
   `enroll_secret`. The agent is now **pending** (no cert yet). The response also
   carries a short-lived, single-use **`ca_ott`** (the step-ca token for step 2;
   null if the provisioner JWK is unconfigured — §7).
2. **Agent → step-ca** — generates a keypair + CSR with the returned `agent_id`
   in the cert CN/SAN and, using the register response's `ca_ott` (a scoped,
   single-use step-ca JWK provisioner token central minted, §7), calls step-ca's
   `POST /1.0/sign` directly to obtain a short-lived client cert. Keys never
   leave the agent; central never proxies the CSR. *(Agent-side; built in P5-T2
   against the API the P5-T2a spike selected — smallstep/certificates `ca` v0.30.2.)*
3. **Agent → `POST /api/v1/agents/{id}/certificate`** `{enroll_secret,
   cert_fingerprint}` — binds the issued fingerprint (pending → **active**). The
   one-time secret closes the window where a guessed pending-agent UUID could be
   hijacked; P5-T2 further hardens this behind the freshly-minted mTLS cert.

A second redemption of the same token is rejected (single-use); an expired token
is rejected. Both are enforced server-side and audited.

## 4. Revocation (kill switch)

Admin → Agents → **revoke** (or `DELETE /api/v1/agents/{id}`) stamps
`revoked_at`. This is an **application-layer denylist** (research §1.4): the
agent is refused on every replication/config request regardless of whether its
short-lived cert is still cryptographically valid. It is **not** a hard delete —
the row and its replication history are retained. Combined with the 24–72h cert
TTL + passive (refuse-to-renew) revocation, this bounds a stolen-cert blast
radius without operating CRL/OCSP infrastructure.

**Hard delete** (Admin → Agents → **delete**, or `DELETE
/api/v1/agents/{id}?purge=true`) removes the row entirely — the cleanup path for
failed-enrollment `pending` rows and decommissioned machines with no data
footprint. Refused (409) while any library or item still references the agent
(replicated data keeps its provenance; revoke those, or delete their libraries
first). Cascades remove the agent's commands/transfers/ledger/reconcile rows;
`libraries.source_agent_id` and `enrollment_tokens.consumed_by` go NULL. Audited
as `agent_deleted`.

**Enrollment tokens**: an unconsumed token deletes freely (`DELETE
/agents/enrollment-tokens/{hash}`); a consumed token's row carries the
`consumed_by` link and needs `?force=true` — the audit event records that link
before the row goes, so the trail survives the cleanup.

## 5. Audit trail

Every mutation writes a `security_events` row (Admin → Audit): `agent_token_minted`,
`agent_token_revoked`, `agent_registered` (ok/rejected + reason),
`agent_cert_bound`, `agent_revoked`. Raw tokens/secrets never appear in the log.

## 6. Agent commands (on-demand instructions, P10-T1)

Separate from the policy/replication channels, the `agent_commands` table is the
queue through which central asks an agent to do ONE thing on demand:

| kind | meaning | who enqueues |
|---|---|---|
| `stat_check` | cheap existence/freshness `stat()` of an agent-hosted item | verify UX (P10-T3) |
| `rehash_check` | strong verify: quick/content hash re-read | verify UX (P10-T3) |
| `stage_upload` | start an agent→central retrieve staging upload | retrieve API (P10-T13) |
| `inventory` | run the W6-D3 inventory collectors on the agent host | inventory UX (W6-D3) |
| `self_update` | run one immediate update check-and-apply (§8.6) — **agent-scoped: `item_id` is absent** | console update button (`POST /agents/{id}/self-update`) |
| `reextract` | sweep the agent's index and re-emit items with a fresh extraction — **agent-scoped** | console re-extract action (`POST /agents/{id}/reextract`) |
| `rehash_sweep` | migrate stale `quick_hash` values in a size band — **agent-scoped**, §14 | console re-hash action (`POST /agents/{id}/rehash-sweep`) |

!!! warning "`rehash_check` is not `rehash_sweep`"
    One word apart, opposite jobs. `rehash_check` is **item-scoped**: central
    asks what one file hashes to right now, the agent answers, nothing is
    written and nothing is replicated. `rehash_sweep` is **agent-scoped**, runs
    for hours, rewrites rows in the agent's local index and emits replication
    events for them. Never reach for one meaning the other.

**Lifecycle.** `pending` → (agent poll delivers) `picked_up` → (agent reports)
`done` / `failed`. A per-minute maintenance sweep flips a stale unpicked
`pending` row or a lease-lapsed `picked_up` row to **`expired`** (kept, not
deleted, so the UI can say "the agent never came back") and re-queues an
unacked delivery back to `pending` (at-least-once), bounded by
`FILEARR_AGENT_COMMAND_MAX_ATTEMPTS`. An admin may **cancel** any pre-terminal
command. `done` / `failed` / `expired` / `cancelled` are terminal + immutable.

**Two auth planes** (both behind `FILEARR_AGENTS_ENABLED`):

- *Operator* — `POST /api/v1/agents/{id}/commands` (enqueue, `write`),
  `GET /api/v1/agent-commands` (list, keyset, filter by agent/state/kind),
  `GET /api/v1/agent-commands/{id}`, `POST /api/v1/agent-commands/{id}/cancel`
  (`write`). Enqueue + cancel are audited (`agent_command_enqueued` /
  `agent_command_cancelled`). **Wave 4 (P6-T4)** swaps the coarse `write` gate on
  enqueue for the path-scoped RBAC `download` action, evaluated *before* the row
  is created.
- *Agent* — `POST /api/v1/agents/{id}/commands/poll` (drain up to `max`
  pending), `.../commands/{cid}/ack` (in-flight lease heartbeat),
  `.../commands/{cid}/complete` (report `{ok, result}`). A poll also refreshes
  `agents.last_seen_at`. This is a PLAIN poll — the held-open long-poll rides
  P5-T4. Per-poll traffic is NOT audited (noise).

**Agent-plane auth — `FILEARR_AGENT_AUTH_MODE` (P5-T6, shipped 2026-07-17).**
Every agent-plane endpoint (commands, replication, reconcile, policy) routes
through `_authenticate_agent`. Three modes:

- `fingerprint` (default) — the **interim** scheme: the agent's bound
  `cert_fingerprint` as a bearer token (the only durable per-agent secret before
  mTLS). *Historical caveat, fixed 2026-07-24:* the fingerprint rotates on cert
  renewal, and central used to keep the enrollment fingerprint forever — every
  agent started 401ing ~⅔ through its first cert lifetime (~32h at the 48h
  default). The agent now **rebinds automatically**: `POST
  /agents/{id}/rebind`, authenticated by proof-of-possession (chain to the
  pinned step-ca root + SAN == agent_id + an ECDSA signature over a
  domain-separated timestamped payload with the leaf key, which survives
  renewal — `backend/filearr/agentcert.py` ⇄ `agent/internal/enroll/rebind.go`).
  The daemon triggers it after every renewal, once at startup (self-heals a
  fleet that drifted before this build), and on any 401/403 from the
  replication/command loops (debounced). `FILEARR_AGENT_AUTH_FINGERPRINT`
  remains as a manual pin/override only; skew tunable:
  `FILEARR_AGENT_REBIND_MAX_SKEW_SECONDS` (default 300).
- `mtls-header` — trust the Caddy `agents.<domain>` site's **already-verified**
  client identity (see `docs/ops/tls.md`): `X-Filearr-Proxy-Auth` must match
  `FILEARR_PROXY_SHARED_SECRET` and `X-Filearr-Agent-San == str(agent_id)`.
  Identity is the **SAN**, which survives cert rotation. The bearer is refused.
  Requires the shared secret (fails closed when unset). *Also fixed
  2026-07-24:* the secondary fingerprint-header check used to 403 after a
  renewal (Caddy forwards the header unconditionally, so stored-vs-forwarded
  always disagreed post-renewal); central now **updates** the stored
  fingerprint on a SAN-matched mismatch — the header comes from a cert Caddy
  already verified, so the stored row is the stale side.
- `both` — transition: mtls-header when the proxy header is present (hard-fails
  on a bad secret/SAN), else bearer. Used during the flip.

**Flip sequence (zero downtime):** set `both` → migrate each agent to
`https://agents.<domain>` (the Go client presents its enrolled cert
automatically) → set `mtls-header`. Full runbook in `docs/ops/tls.md`.

**New agents on an mtls-header central (2026-08-16):** enrolment cannot go
through `agents.<domain>` — that site *requires* a client certificate and the
agent does not have one until step-ca has signed it — so the install one-liner
always bootstraps via the console URL. Set **`FILEARR_AGENT_PLANE_URL`**
(`https://agents.<domain>`; the Unraid and Proxmox deployers set it) and the
register response carries it as `agent_plane_url`: the agent persists it as
its daemon central (`state.json` `central_url`, with the console URL kept in
`enroll_url`), so it lands on the mTLS plane from its first start with no
per-agent repoint. A sidecar/env `central_url` that still names the console
URL (the install script writes exactly that) is treated as the bootstrap
value, not an operator repoint; a URL differing from both still wins.

**Tunables** (`FILEARR_AGENT_COMMAND_*`): `TTL_SECONDS` (default 3600, "hours not
minutes"; per-kind defaults are P10-T7), `TTL_MAX_SECONDS` (enqueue override
clamp), `LEASE_SECONDS` (unacked-delivery redelivery window, default 300),
`MAX_ATTEMPTS` (5), `POLL_MAX` (50), `PAYLOAD_MAX_BYTES` / `RESULT_MAX_BYTES`
(size caps — a hostile/buggy caller cannot bloat a row). The UI surfaces an
agent's commands via Admin → Agents → **commands** (state chips + cancel).

## 7. step-ca provisioner claims + the JWK secret + the CA proxy (P5-T2)

P5-T2 (central half) mints the `ca_ott` on register. Three operator steps make
it work end to end.

### 7.1 Provisioner claims

> **Remote management changes everything here (live finding 2026-07-18).** Our
> compose sets `DOCKER_STEPCA_INIT_REMOTE_MANAGEMENT=true`, and in that mode
> step-ca keeps provisioners in its **admin database** — `authority.provisioners`
> in `ca.json` is absent, so editing `ca.json` (the pre-2026-07-18 instruction
> below) does nothing. Set the claims through the admin API instead (auto-init's
> initial admin is subject `step` on the init provisioner; password = the CA
> password file):
>
> ```bash
> docker compose exec -T step-ca step ca provisioner update filearr-agents \
>   --x509-min-dur=24h --x509-default-dur=48h --x509-max-dur=72h \
>   --allow-renewal-after-expiry \
>   --admin-subject=step --admin-provisioner=filearr-agents \
>   --admin-password-file=/home/step/secrets/password \
>   --ca-url https://localhost:9000 --root /home/step/certs/root_ca.crt
> ```
>
> The Proxmox deploy script does this automatically. The `ca.json` shape below
> is kept for reference / non-remote-management CAs only.

The bare provisioner the compose `DOCKER_STEPCA_INIT_*` env creates issues certs
but does not encode the spike's TLS-lifetime ruling. Set its claims to:

```jsonc
// step-ca config/ca.json -> authority.provisioners[JWK "filearr-agents"]
{
  "type": "JWK",
  "name": "filearr-agents",
  "key": { /* public JWK — auto-populated */ },
  "encryptedKey": "...",              // the private JWK, JWE-encrypted (see 7.2)
  "claims": {
    "minTLSCertDuration": "24h",
    "defaultTLSCertDuration": "48h",
    "maxTLSCertDuration": "72h",
    "allowRenewalAfterExpiry": true   // BOUNDED grace for long-offline agents
  }
}
```

`allowRenewalAfterExpiry` lets a long-offline agent renew a just-expired cert
over mTLS instead of re-enrolling; it is the CA-side half of the re-enrollment
gap the re-issue endpoint (7.3) covers on the central side. Edit `ca.json` in
the `stepca_data` volume and restart step-ca.

### 7.2 Extract the provisioner private JWK -> `FILEARR_CA_PROVISIONER_JWK`

Central signs the OTT with the provisioner's **decrypted private** JWK. step-ca
stores it JWE-encrypted (`encryptedKey`) under the provisioner password.

**Where `encryptedKey` lives depends on remote management** (see §7.1): with it
on (our compose default) it is NOT in `ca.json` — fetch it from the CA's public
`/provisioners` endpoint (serving the JWE publicly is by design; that is how
`step ca token` works client-side — only the password can open it):

```bash
# in the CT (the Proxmox deploy script automates exactly this)
ENC=$(curl -sk https://localhost:9000/provisioners \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); \
      print(next(p["encryptedKey"] for p in d.get("provisioners", d if isinstance(d,list) else []) \
      if p.get("name")=="filearr-agents" and p.get("type")=="JWK"), end="")')
printf '%s' "$ENC" | docker compose exec -T step-ca \
  step crypto jwe decrypt --password-file /home/step/secrets/password
# -> {"kty":"EC","crv":"P-256","kid":"...","x":"...","y":"...","d":"..."}
```

**Which password?** (live finding 2026-07-18): with remote management the
provisioner key is encrypted under the CA **administrative password** — printed
ONCE in the first-boot log (`docker compose logs step-ca | grep -i "password
is"`), and NOT the same as `secrets/password` (the CA-key password). The deploy
script tries `secrets/password`, then `secrets/admin_password`, then recovers
the log-printed password — persisting it as `secrets/admin_password` (0600, in
the CA volume) so nothing ever depends on container-log retention again. If the
first-boot log is gone AND `admin_password` was never persisted, the password
is unrecoverable — rotate the provisioner key (create a new keypair, `step ca
provisioner update filearr-agents --private-key=...`, put the new plaintext
private JWK in `.env`).

Paste the result into `FILEARR_CA_PROVISIONER_JWK` (single-operator model: env is
acceptable; **rotate** by generating a new provisioner key, updating `ca.json`,
and replacing the env value). It is a **secret** — same class as
`FILEARR_SECRET_KEY`; Filearr never logs it (only *that* it is unset/malformed),
and validates its shape (EC P-256, private) on first use.

**Fail-safe.** If `FILEARR_CA_PROVISIONER_JWK` is unset OR malformed, registration
still succeeds but `ca_ott` is **null** — a bad key never takes enrollment down;
agents simply cannot obtain certs until it is fixed, then use the re-issue
endpoint (7.3) to hand a fresh OTT to already-registered agents. Central emits
`agent_ca_ott_minted` (agent_id + `jti` only — never the token) on every mint.

### 7.3 Re-issue endpoint (re-enrollment recovery)

`POST /api/v1/agents/{id}/ca-ott` (admin scope) mints a **fresh** OTT for an
existing **pending or active** agent — the operator-driven recovery path for an
agent long offline past its cert TTL, or one that registered before the JWK was
plumbed.

The agent-side half is **`filearr-agent reissue -ott <ott>`** (2026-08-05): it
uses the OTT to obtain a fresh leaf from step-ca while keeping the on-disk
identity (agent id, central URL, CA pin) verbatim — no re-enroll, no new agent
row, replication watermark preserved — then re-binds the new fingerprint with
central. The OTT is short-lived (`FILEARR_CA_OTT_TTL_SECONDS`, default 300 s),
so mint it immediately before running reissue:

```bash
# on any admin box:
curl -s -X POST http://<ct-ip>:8484/api/v1/agents/<agent-id>/ca-ott
# on the agent host, within the TTL:
filearr-agent reissue -ott <the minted ott>
```

The running daemon picks the new certificate up on its next renewal check and
rebind trigger; `filearr-agent service restart` makes it immediate. An OTT
minted for a DIFFERENT agent is refused (SAN check) and leaves the on-disk
identity untouched. A **revoked** agent is refused (409); an unknown id is 404; if the
provisioner JWK is unconfigured the endpoint returns 503 (its only job is to
mint). Audited by `jti`.

### 7.4 CA proxy: SNI / L4 passthrough (do NOT L7-terminate)

**The CA must NOT be L7-proxied.** step-ca's `/renew` authenticates via the
client cert on the direct TLS connection; an L7 terminator silently breaks it
(the `/1.0/sign` enroll call would survive L7, but renewal will not). Route a
dedicated hostname (e.g. `ca.<domain>`) as **SNI-based L4/TCP passthrough** to
step-ca:9000, never through an L7 reverse proxy.

> **P5-T6 update (2026-07-17): this is now solved IN-CT — no external proxy
> needed.** In `acme-dns` TLS mode (`docker/caddy/Caddyfile.acme`, see
> `docs/ops/tls.md`) the CT's own Caddy carries a caddy-l4 **listener wrapper** on
> 443 that peeks the TLS ClientHello and raw-TCP-proxies `ca.<domain>` straight to
> `step-ca:9000` (the CA keeps its own TLS), while every other SNI falls through to
> the HTTP app. The external nginx front is out of the path entirely; there is no
> upstream `stream {}` block to maintain. The constraint above still holds — it is
> now enforced by the l4 route rather than by hand.

### 7.5 Go agent implementation facts (P5-T2 shipped, 2026-07-16)

The Go half lives in `agent/` (`cmd/filearr-agent` + `internal/enroll`,
module `github.com/filearr/filearr/agent`, Go 1.26, sole production dep
`github.com/smallstep/certificates v0.30.2`). `filearr-agent enroll` runs the
full §3 handshake; `filearr-agent run` starts the renewal daemon (2/3-TTL +
±10% jitter, capped exponential backoff, mTLS `/renew`, best-effort root
refresh after every renewal + an out-of-band `RefreshRoots` hook for the
future CA-rotation signal). Facts verified against a real in-process step-ca
0.30.2 authority (recorded here so nobody re-litigates them):

- **`cert_fingerprint` = lowercase hex SHA-256 of the leaf DER.** Central
  stores it verbatim (no format enforcement — `agentsync.bind_agent_certificate`
  equality-compares only), and this matches step-ca's own root-fingerprint
  convention, so `step certificate fingerprint` output is directly comparable.
- **CSR↔OTT SAN enforcement is REAL:** step-ca's JWK provisioner rejects (403)
  any CSR whose SANs differ from the OTT's `sans`. The agent uses
  `ca.CreateSignRequest(ott)` which derives CN/SANs from the OTT itself, so
  CSR==OTT holds by construction. A bare-UUID agent_id classifies as a **DNS
  SAN** (`x509util.SplitSANs`).
- **Clock skew: ±1 minute leeway** on OTT validation (`ValidateWithLeeway`);
  +30 s future `nbf`/`iat` accepted, +2 min rejected.
- **OTT replay rejected by `jti`** ("token already used", 401). Audience
  matching is port-insensitive (central's port-bearing `aud` matches).
- Windows caveat: the key file's 0600 mode doesn't map to an ACL — effective
  protection is the data dir's inherited ACL (hardening item, tracked in
  `certstore.go`).

## 8. Agent self-update: signed manifest + `auto_update` gating (P5-T7)

Agents self-update from an operator-signed manifest. The signing **private key
lives only on your signing machine** (never on central, never in the repo — see
`agent/README.md` for keygen + the `-ldflags` public-key pin), so a compromised
central cannot push a wrongly-signed binary (research §8: central is untrusted
for update integrity — the agent verifies the Ed25519 signature against its
build-time pinned key).

**Production public key** (generated 2026-07-17 on the dev host; private key in
`~/.filearr-signing`, vault-backed; PUBLIC material — safe to publish). Every
production agent build must pin it:

```bash
go build -ldflags "-X github.com/filearr/filearr/agent/internal/update.PublicKeyBase64=j0hTanu7jdT44h7pwPzb5vv0juSDmJMCAyi8tV33/wQ=" ./cmd/filearr-agent
```

A binary built WITHOUT the pin refuses all updates (fail-closed) — fine for dev,
wrong for the fleet. If the key is ever regenerated (`keygen --force`), every
deployed agent must be rebuilt/redeployed with the new pin before it will accept
another update.

### 8.1 Settings

```bash
# .env
# Where uploaded artifact BINARIES live (manifests go in the agent_releases table).
# Default: {config_dir}/agent-releases  (i.e. /config/agent-releases in the LXC).
FILEARR_AGENT_RELEASES_DIR=/config/agent-releases
# Hard ceiling on one uploaded artifact (bytes).
FILEARR_AGENT_UPDATE_MAX_ARTIFACT_BYTES=536870912   # 512 MiB default
```

There is **no release-staging setting**. P13 (2026-08-11) removed binary-release
staging along with `agents.rollout_group`: every uploaded release is generally
visible once all its artifacts are present, and who takes it is decided by the
`auto_update` key in a config group (§8.5) plus the per-agent `self_update`
command. Attaching releases to the config tier engine (§9.3) is a roadmap item.

### 8.2 Operator flow

1. **Build + sign** three platforms and produce `manifest.json` (see
   `agent/README.md` — `filearr-release keygen` once, then build with the pinned
   public key, then `filearr-release sign`).
2. **Register** the signed manifest (admin scope):
   `POST /api/v1/agent-releases` (body = `manifest.json`).
3. **Upload** each artifact binary (admin scope), verified against the manifest
   sha256/size: `PUT /api/v1/agent-releases/{version}/artifacts/{filename}` with
   the raw file as the body. A release is only offered once **every** manifest
   artifact is present.
4. **Decide who takes it, before you upload.** With `auto_update` left on
   everywhere, the whole fleet sees the release on its next poll (default 6h;
   `FILEARR_AGENT_UPDATE_POLL_INTERVAL`, or the `update_poll_interval_seconds`
   policy key to retune a group live). To stage it: set `auto_update: false`
   in the **Global** config group and `true` in a small higher-priority group
   holding the machines you want on it first (§8.5).
5. **Watch confirmations.** `GET /api/v1/agent-releases` returns each release's
   `confirmed_count` and a per-agent `agent_version` rollup — the §6.3 "which
   version has each agent confirmed" query. An agent reports its running version
   on every manifest poll; that report IS the confirmed-version signal (a polling
   agent has, by definition, booted + passed its 60s health window).
6. **Widen** by moving machines into the enabled group, or by flipping the
   Global `auto_update` back on once the first wave is healthy.

### 8.3 Rollback semantics

A newly-swapped binary is on trial: the agent writes a boot-counter, then on each
launch runs a 60s health window (local index opens + a central contact succeeds
or fails cleanly, no panic). On pass it clears the counter, deletes the `.old`
binary, and confirms its version. If the new binary crashes through **3 launch
attempts** without ever passing, the next launch **automatically restores the
previous binary** and re-execs it (systemd-boot boot-assessment pattern, research
§5.3). Run the agent under a service manager with restart-on-failure (systemd
`Restart=on-failure` + `StartLimitBurst`, a Windows Service failure action, or
launchd `KeepAlive`) so a crashed agent is relaunched to trigger the rollback.

A `sha256` mismatch on download or an invalid signature **refuse the update**
(fail-closed) rather than swapping. An unpinned build refuses every *signed*
manifest it cannot verify — but see §8.4 for the dist channel it CAN use.

### 8.4 Central-version updates (the dist channel, 2026-08-05)

Central also tracks the **published central console version**: the agent-dist
bake inside the Docker image (`/app/agent-dist`, the same binaries the
first-install scripts serve, stamped e.g. `1.5.0-1a2b3c4` — see §8.4.1). When an agent's
update-manifest poll finds **no covering signed release** but the dist version
*differs* from what the agent runs, central serves an **UNSIGNED** manifest
derived from the dist bake (artifacts ride the same per-release download path
as a virtual release). Deploying a new central image is therefore all it takes
for the fleet to converge on that image's agent build.

Trust model split, enforced on both ends:

- **Unpinned builds** (everything installed via the agent-dist scripts) accept
  the unsigned manifest — their trust root is the authenticated TLS channel to
  their enrolled central plus the manifest sha256, exactly the trust their
  original install carried. The agent logs a WARN when it does this.
- **Key-pinned builds** refuse unsigned bits (fail-closed, unchanged) and
  advertise `key_pinned=true` on the poll so central never offers them the
  dist channel — they update only via the signed §8.2 flow.

Because decorated build stamps (`1.5.0-3303638`, or legacy `main-1a2b3c4`)
have no defined ordering against each other, the dist channel uses **string
inequality** ("track the exact published central build"), while clean release
tags keep semver ordering (never downgrade) — `update.ShouldApply` / central's
`_should_offer`, kept in sync.

### 8.4.1 Version stamping: `agent/VERSION` is the single source of truth (2026-08-07)

Every build path stamps `main.Version` from the **`agent/VERSION`** file plus a
build discriminator — bump that one file to move the whole ecosystem:

| Build path | Stamp shape | Example |
|---|---|---|
| CI (both Docker images) | `<VERSION>-<sha7>` | `1.5.0-1a2b3c4` |
| Proxmox deploy (agent-dist bake) | `<VERSION>-<hash7>` (source content hash) | `1.5.0-3303638` |
| `build_windows_agent.ps1` (local) | `<VERSION>-<sha7>` | `1.5.0-1a2b3c4` |
| Signed releases (`filearr-release sign`) | operator-chosen clean tag | `1.5.0` |

Before this, the three paths disagreed wildly (live fleet showed `main`,
`1.4.0` and `0.0.0-dev` simultaneously): the deploy never passed the build
arg (dist bake = `0.0.0-dev`), CI stamped the **branch name**, the agent
image appended a space + date that `_SAFE_VERSION` rejects, and the Windows
script had `v1.4.0` **hardcoded** — a rebuilt binary re-reported 1.4.0
forever. Note the Windows script's output is **key-pinned** (it bakes the
release public key), so it updates ONLY via signed §8.2 releases — cutting a
new signed release with a version above the fleet's is what actually moves
those agents; central image redeploys never will.

### 8.5 Gating + staged rollout: the `auto_update` key

The update-manifest poll is gated **server-side** by the effective `auto_update`
key (absent = **true**, preserving historic behavior). When false, the poll
answers 204 — the agent still reports its version (the console still sees
drift), it just isn't offered anything. It gates both channels identically
(signed releases §8.2 and the dist channel §8.4).

Staged rollout recipe, same for either channel:

```bash
# 1. hold the fleet
PATCH /api/v1/agents/config-groups/<global-id>   {"policy": {"auto_update": false}}
# 2. a small, high-priority group that lets its members through
POST  /api/v1/agents/config-groups
      {"name": "update-first", "priority": 900, "policy": {"auto_update": true}}
# 3. put the pilot machines in it
PUT   /api/v1/agents/<agent-id>/config-groups    {"group_ids": ["<update-first-id>"]}
```

Config resolution is a per-key layered merge (§9.1), so `update-first` needs to
state **only** `auto_update` — everything else those machines run still comes
from Global and their other groups. Widen by adding members, or by setting
Global back to `true` once the pilot wave is healthy.

### 8.6 Console: update badge + per-agent trigger

`GET /api/v1/agents` now surfaces `update_available` / `update_target` (newest
covering ready signed release, else the differing dist version) and
`update_pending` per agent. The Agents page shows an **"update available"**
badge next to the version and an **update** action that queues a
`self_update` command via `POST /api/v1/agents/{id}/self-update` (write scope;
audited as `agent_update_triggered`; 409 when up-to-date or already queued).
The agent picks it up at its next command check-in (default 60s), runs one
immediate check-and-apply, and completes the command with
`{"status": "applying", "version": ...}` just before the swap — the version it
reports on its next manifest poll is the real confirmation. An in-flight
`self_update` command **overrides** an `auto_update: false` policy (the click
is the authorization), so the button works on gated fleets too. While one is
queued the console shows **"update queued"** and hides the button; agents with
no update available show no button at all.

**Containerized agents (Docker/Unraid) are flagged, never offered**
(2026-08-07): the agent image advertises `container: true` in its capability
poll (`FILEARR_AGENT_CONTAINER=1`, `/.dockerenv` as fallback for hand-rolled
containers), because an image updates by **pulling a new image** — a binary
swapped inside a container dies on the next recreate. For such agents the
console shows a **"newer image available"** badge (no update button), the
trigger endpoint returns 409 with a pull-the-image message, the manifest poll
always answers 204, and the agent itself refuses to apply as a final
belt-and-braces (it logs the offered version instead). Deliberate in-container
swaps remain possible: set `FILEARR_AGENT_CONTAINER=0` +
`FILEARR_AGENT_SELF_UPDATE=true` (restart policy must relaunch on exit 20).

### 8.7 Key rotation without a fleet rebuild (dual-pin)

`PublicKeyBase64` accepts **multiple comma-separated keys** — a manifest
verifying against ANY pinned key is accepted. Rotation is therefore a rolling
three-step, all through the normal update channel:

1. Generate the next keypair (`filearr-release keygen` to a NEW directory).
   Build + sign a release with the **old** key whose binaries pin BOTH:
   `-X ...PublicKeyBase64=<current>,<next>`. Roll it out normally.
2. Once the fleet has confirmed that release (`GET /api/v1/agent-releases`
   rollup), start signing with the **next** key. Old-pinned stragglers refuse
   it (fail-closed) and show up in the version rollup — update them first.
3. On the following release, drop the old key from the pin. Retire/destroy
   the old private key.

A malformed entry anywhere in the comma list fails the WHOLE pin set at
startup (never silently drop a key an operator thought was active).

### 8.8 Key custody + public build provenance

**Custody:** keep the signing private key OFF disk where possible — a YubiKey
(PIV) or a cloud KMS the signing step calls, with the vault backup as the
recovery path. The dual-pin rotation above is what makes a custody upgrade
cheap: generate the new key ON the hardware, rotate to it, retire the file
key. The key never needs to exist on a laptop filesystem again.

**Provenance (Sigstore, keyless):** every published image is attested by the
release workflow via `actions/attest-build-provenance` — a Fulcio-signed,
Rekor-logged statement binding the image digest to this repo + workflow +
commit, with **no private key in custody at all**. Verify any pull:

```bash
gh attestation verify oci://ghcr.io/pwsh/filearr:latest       -R pwsh/filearr
gh attestation verify oci://ghcr.io/pwsh/filearr-agent:latest -R pwsh/filearr
```

This covers the agent-dist first-install binaries too — they are baked inside
the attested main image, so the image attestation is their provenance chain.
The two mechanisms are complementary, not redundant: Sigstore proves *where a
build came from* (and moves trust to GitHub+Sigstore infrastructure); the
pinned Ed25519 manifest signature proves *the operator authorized this exact
update* even against a compromised central or CI. High-assurance fleets keep
both; OS-level trust (Windows Authenticode via Azure Trusted Signing, macOS
notarization) is the third, deferred layer for when binaries target users
outside the operator's own trust domain.

## 9. Configuration groups: the single configuration mechanism (W6-D2, P13)

A **config group** is a named row holding two document sections — `settings`
(typed, unknown keys rejected) and `policy` (permissive, unknown keys preserved)
— an integer `priority`, and its own version history. Since P13 (2026-08-11)
this is the ONLY configuration mechanism: the old policy scopes
(`global` / `group:<name>` / `agent:<uuid>`, whole-document replacement),
`agents.rollout_group`, and the `/agent-policies/*` admin surface are gone.

A permanent **Global** group (`is_system=true`, `priority=0`, name and priority
immutable, undeletable) applies to every agent implicitly — no membership rows.
Agents can be members of any number of other groups
(`agent_config_group_members`).

Admin surface (all `admin` scope, all audited, all 404 when
`FILEARR_AGENTS_ENABLED=false`):

```bash
# Create a group (both sections validated; see the schema below)
curl -s -X POST http://<ct-ip>:8484/api/v1/agents/config-groups \
  -H 'content-type: application/json' -d '{
    "name": "office-workstations",
    "description": "Windows desktops, documents + downloads",
    "priority": 100,
    "settings": {
      "log_level": "info",
      "scan_selections": [
        {"preset": "user-documents", "paths": [], "enabled": true},
        {"preset": "downloads",
         "paths": ["%USERPROFILE%/Downloads"],
         "exclude_regex": ["\\.tmp$"],
         "enabled": true}
      ],
      "inventory": {"enabled": true, "collectors": ["stat", "owner"]},
      "scan_schedule_cron": "0 3 * * *"
    },
    "policy": {"extract_enabled": true, "extract_exif": false}
  }'

curl -s .../api/v1/agents/config-groups          # list, in MERGE ORDER (+member_count,
                                                 #   current_version, active_rollout)
curl -s .../api/v1/agents/config-groups/<id>     # get + latest 20 versions
curl -s -X PATCH  .../config-groups/<id> -d '{"policy": {...}, "note": "why"}'
curl -s -X DELETE .../config-groups/<id>         # 409 if is_system
curl -s .../config-groups/<id>/history           # newest first, ?before=<version>, cap 100
curl -s -X POST .../config-groups/<id>/rollback -d '{"version": 4}'

# Membership: PUT replaces the agent's ENTIRE explicit group set
curl -s -X PUT .../agents/<agent-id>/config-groups -d '{"group_ids": ["<id>", "<id2>"]}'
curl -s -X PUT .../agents/<agent-id>/config-groups -d '{"group_ids": []}'

# What does this machine actually get, and from where?
curl -s .../agents/<agent-id>/effective-config
```

Passing Global's id to the membership PUT is a **400** — it is implicit, so
accepting it would imply it could also be omitted. Deleting Global is a **409**;
so is renaming or re-prioritising it. Deleting any other group cascades its
membership rows and cancels any live rollout on it.

`PATCH` is the only publish path: any `settings`/`policy` change snapshots a new
version. Each section **replaces wholesale** when supplied — authoring is
replace, layering is what happens across groups (deep-merging on write too would
leave an operator unable to unset a key).

### 9.1 Resolution: per-key layered merge

`agent_config.resolve_effective_config()`:

1. Load Global + the agent's member groups ordered by `(priority, name, id)`.
2. Per group, pick the version THIS agent gets — a running rollout whose active
   tier covers the agent's bucket hands over `target_version`, otherwise
   `current_version`.
3. Merge each section key by key in that order. A later group overrides only the
   keys it sets. The merge is **shallow** at each section's top level: a nested
   object (`inventory`, `scan_selections`) replaces wholesale.
4. Compose the frozen wire document, the generation and the content hash.

Equal priorities are legal (tie-break by name, then id) so inserting a group
between two others is never a renumbering exercise.

### 9.2 Versions and generations

`agent_config_group_versions` carries a per-group `version` (from 1) and a
fleet-wide `seq` identity column. The **generation** delivered to an agent is
`max(seq)` over the snapshots that composed its document — monotonic, so it only
moves forward, and `agents.config_generation_applied` stores what the agent
echoed back via `?applied=`.

Versioning is forward-only: rollback copies an old snapshot into a NEW version
and publishes it immediately (no rollout option — reverting a breaking config
should not wait), cancelling any live rollout on that group.

### 9.3 Phased rollouts

`PATCH`ing a group with a `rollout` block phases the new version in instead of
switching every member at once:

```bash
curl -s -X PATCH .../api/v1/agents/config-groups/<id> -d '{
  "policy": {"extract_ocr": true},
  "note": "OCR on the filers",
  "rollout": {
    "tiers": [{"percent": 10, "delay_minutes": 0},
              {"percent": 50, "delay_minutes": 120},
              {"percent": 100, "delay_minutes": 240}],
    "starts_at": "2026-08-12T02:00:00Z"
  }
}'

curl -s .../api/v1/agents/config-rollouts                       # live ones (?status= for the rest)
curl -s -X POST .../agents/config-rollouts/<id>/promote         # advance now (409 if not running)
curl -s -X POST .../agents/config-rollouts/<id>/cancel          # stop shipping it
```

- Tiers: 1..5 entries of `{percent 1..100, delay_minutes >= 0}`, percents
  **strictly ascending**, last must be **100** (422 otherwise). A rollout with no
  document change is also a 422.
- Bucketing: `sha256(agent.id.bytes)[:4] % 100`, so tier *P* covers buckets
  `< P`. Derived, not stored — a fleet that grows mid-rollout keeps a uniform
  slice with no backfill.
- `delay_minutes` of tier N is the wait after tier N-1 activated; tier 0's counts
  from the rollout start. `starts_at` NULL = the next minute tick.
- The engine is `_advance_config_rollouts()` inside the existing every-minute
  worker tick: `scheduled` → `running` at tier 0 → one tier per tick as delays
  elapse → `completed` at the last tier, which finally moves
  `group.current_version` to `target_version`. It is skipped entirely while
  central is in maintenance mode and catches up (one tier per tick) afterwards.
- **Cancel means fall back**: `current_version` is untouched, so covered agents
  return to it on their next poll (~60 s). To keep the new version, promote to
  completion instead.
- Partial UNIQUE on `group_id WHERE status IN ('scheduled','running')` — a second
  live rollout on the same group is a 409.

Binary releases do NOT ride this engine yet (§8.1); that is a roadmap item.

### Settings schema (typed, versioned)

The `settings` object is Pydantic-validated at the API. **Unknown top-level keys
are rejected with 422** so a typo never silently no-ops (contrast the same
group's `policy` section, which preserves unknown keys for forward-compat). Whole doc
is capped at 64 KiB (422 beyond). All keys optional:

| Key | Type | Notes |
| --- | --- | --- |
| `log_level` | `error`\|`warn`\|`info`\|`verbose`\|`debug` | agent log verbosity |
| `scan_selections` | list of selection objects | see below (max 100) |
| `inventory` | `{enabled: bool, collectors: [str]}` | collector names are free strings — W6-D3 defines the vocabulary; central only caps count/length |
| `scan_schedule_cron` | cron string | cronsim-validated, exactly like `library.scan_cron` |

A **scan selection**: `{preset, paths, include_regex, exclude_regex, enabled}`.

- `preset` — one of the W6-R1 preset names (or `null`):
  `user-documents`, `user-media`, `user-profiles-full`, `downloads`,
  `server-data`, `custom`. See `docs/research/agent-inventory-presets.md` for the
  per-OS folder expansions each resolves to.
- `paths` — explicit path specs. A spec **MAY** carry env tokens
  (`%USERPROFILE%`, `$HOME`, `~`) and glob segments (`/home/*/documents`,
  `/data/{a,b}/[abc]*`). Central **validates syntax only** (non-empty, balanced
  brackets/braces) and **never resolves a path** — final resolution is agent-side
  and per-OS (Windows known-folder API, Linux `user-dirs.dirs`, macOS TCC), per
  W6-R1.
- `include_regex` / `exclude_regex` — refine matches. Central compiles each with
  Python `re` as a **sanity gate only**; the authoritative match engine is the Go
  agent's RE2/`regexp` (a pattern valid in one is not guaranteed valid in the
  other, but the gate catches the common typo class).
- `enabled` — gates the whole selection (default `true`).

Example specs that PASS validation: `%USERPROFILE%/Documents`, `$HOME/documents`,
`~/Documents`, `/home/*/documents`, `/data/{a,b}/[abc]*`. A spec like
`/home/[user` (unbalanced bracket) is a 422.

### Delivery (the wire shape is FROZEN)

The merged document rides `GET /agents/{id}/policy` (§6) exactly as before:
merged `policy` keys at the top level, merged `settings` under a top-level
`group` section, the three lifted local-surface keys
(`web_ui_enabled`, `local_access_enabled`, `auth_required` — **the `settings`
value wins**, `null` = inherit), and the server-injected `taxonomy_version`.
Deployed Go binaries needed zero changes for P13.

What the fields MEAN changed:

```text
{"scope": "groups", "version": <generation>, "policy": {…}}
ETag: "groups/<generation>/h:<sha256[:12] of canonical doc>/t:<taxonomy_version>"
```

`scope` is now the literal constant `"groups"` (one resolution scheme, so the
field carries no information and exists only so old binaries keep parsing), and
`version` is the generation (§9.2) rather than a per-scope policy version.

Three independent things invalidate the cache and all three ride the ETag: any
contributing group publishing (generation), the merged content changing (hash —
which is what catches a **membership or priority edit**, neither of which moves a
version number), and a taxonomy edit. `?applied=<generation>` stamps
`agents.config_generation_applied` + `last_seen_at`.

An agent never 404s here: with no explicit groups it still resolves Global, and
with no Global row (only reachable mid-migration) it gets an empty document
rather than an error.

Admin-side, `GET /api/v1/agents/{id}/effective-config` returns the same document
minus `taxonomy_version`, plus the ordered contributor list
(`version_used`, `via_rollout`), per-key `provenance`
(`"<section>.<key>" -> {group_id, group_name, version}`), and
`confirmed_generation` for the published-vs-enforced comparison.

## 10. Console installer distribution (W6-D2)

`POST /api/v1/agents/installer-config` (`admin` scope, audited) mints an
enrollment token (§3 machinery) and returns the **complete sidecar** the console
agent installer consumes (`filearr-agent.json`), plus token metadata and per-OS
install hints:

```bash
curl -s -X POST http://<ct-ip>:8484/api/v1/agents/installer-config \
  -H 'content-type: application/json' -d '{
    "agent_name": "lab-01",
    "config_group_ids": ["<group-id>", "<group-id-2>"],
    "log_level": "info",
    "central_url_override": "https://filearr.example.com",
    "ttl_seconds": 3600
  }'
```

Response (FROZEN contract for the UI, W6-D4):

```json
{
  "sidecar": {
    "central_url": "https://filearr.example.com",
    "enrollment_token": "fae_…",        // raw, show-once
    "agent_name": "lab-01",
    "config_group_names": ["office-workstations", "low-power"],  // by NAME
    "config_group": "office-workstations",  // first name, for shipped binaries
    "log_level": "info"
  },
  "token_hash": "…",                     // for show/revoke in the UI
  "expires_at": "2026-07-18T…Z",
  "install_hint": {
    "windows": "irm https://filearr.example.com/api/v1/agent-dist/install.ps1 -OutFile install-agent.ps1; .\\install-agent.ps1   # elevated shell; add -Token <token> if filearr-agent.json is not beside it",
    "linux":   "curl -fsSL https://filearr.example.com/api/v1/agent-dist/install.sh | sh   # add: -s -- -t <token> if filearr-agent.json is not in the cwd",
    "macos":   "curl -fsSL https://filearr.example.com/api/v1/agent-dist/install.sh | sh   # add: -s -- -t <token> if filearr-agent.json is not in the cwd"
  }
}
```

Notes:

- `central_url` is the request base URL unless `central_url_override` is set.
- `config_group_ids` (if given) must all exist (422 otherwise) and are emitted in
  the sidecar **by name** in `config_group_names`. The membership is ALSO recorded
  on the minted enrollment token (`enrollment_tokens.config_group_names`), which
  is the authoritative half — central applies it at register whatever the agent
  sends. `config_group` repeats the first name because shipped agent binaries
  parse exactly that key and pass it to `/agents/register`; dropping it would
  silently strip the group from every install done with a current binary.
- Global is implicit and never listed. An **unknown name at register is
  fail-safe**: the agent enrolls into whichever named groups do exist (Global
  always), and the register response carries a `config_group_warning` naming the
  ones it could not resolve — enrollment is never blocked.
- `POST /api/v1/agents/enrollment-tokens` takes the same list directly as
  `config_group_names` (see `scripts/manage-windows-agent.ps1 -ConfigGroup`,
  repeatable).
- The install hints reference the **first-install distribution surface**
  `/api/v1/agent-dist`: cross-compiled binaries baked into the central Docker
  image (`/app/agent-dist`; override `FILEARR_AGENT_DIST_DIR`) plus generated
  `install.sh` / `install.ps1` scripts templated with the central URL. The
  surface is deliberately **unauthenticated** behind the `agents_enabled`
  feature gate — the binaries are public AGPL artifacts, the scripts verify
  sha256 against the manifest, and joining the fleet still requires an
  operator-minted enrollment token. (The §8 release-artifact path is
  agent-cert-authenticated and serves *self-update*, not first install.)
  `GET /api/v1/agent-dist` returns the manifest: version + per-platform
  `{filename, os, arch, size, sha256, url}`;
  `GET /api/v1/agent-dist/<filename>.sha256` returns just the digest.
- Audit records the token hash + config group only — **never** the raw token.

## 11. Extensible inventory framework (W6-D3)

An `inventory` command walks an expanded set of roots and runs a set of per-file
**collectors**, returning one NDJSON record per surviving entry plus an
always-present summary. New inventory COMPOSITIONS (a preset + collector set) are
authored centrally and need NO agent redeploy — the agent advertises the
vocabulary it supports and an admin composes against it.

**Command shape** (`kind: inventory`, created via the EXISTING command-creation
endpoint — no new enqueue surface; the `kind` CHECK gained `inventory` in
migration `c7d9e1f3a5b8`):

```jsonc
// POST /api/v1/agents/{id}/commands
{
  "kind": "inventory",
  "item_id": "<uuid>",          // required by the shared endpoint (not item-scoped work)
  "payload": {
    "collectors":    ["stat", "owner", "perms", "placeholder"],
    "preset":        "user-documents",  // W6-R1 preset, or null
    "paths":         ["%USERPROFILE%\\Projects", "/srv/*/data"],
    "include_regex": ["\\.docx?$"],     // RE2 (Go regexp), applied to rel paths
    "exclude_regex": ["(?i)/temp/"],
    "max_entries":   100000,
    "max_depth":     0                  // 0 => unlimited
  }
}
```

**Path-expansion engine** (`agent/internal/pathspec`, SHARED — also the W6-D2
group scan-root consumer): per spec, (1) env-token expansion — Windows `%VAR%`,
POSIX `$VAR`/`${VAR}`, leading `~`; an unset variable fails that spec (recorded,
never a literal walk); (2) glob — a spec with `* ? [` is `filepath.Glob`-expanded
(existence-filtered; this is how `C:\Users\*\Documents` / `/home/*/docs` fan out),
else it is a single literal root; (3) a global fan-out cap (default 10 000 roots)
truncates + flags rather than erroring. RE2 include/exclude filters apply to rel
paths during the walk. **Braces `{}` are NOT expanded** (stdlib Glob has no brace
expansion — documented divergence from central's balance-only check).

**Presets** (W6-R1) resolve to per-OS specs INSIDE the agent: Windows known
folders via `SHGetKnownFolderPath` (KFM/OneDrive-redirect-correct) + profile-glob
fallback; Linux `~/.config/user-dirs.dirs` (locale-translated names honored,
`$HOME` fallback when absent); macOS fixed paths. `server-data` → `/srv`,
`/var/www` on Linux only. The walk reuses the vetted exclusion bundles
(`system_files`, `os_metadata`, `caches_temp`, `node_modules_build`, +
default-on `hidden_dotfiles`).

**Collectors** (v1 built-ins; all metadata-only — NEVER open content):

- `stat` — size, mode, mtime, + access/creation times where cheap (per-OS).
- `owner` — POSIX uid/gid + resolved names; Windows owner SID
  (`GetNamedSecurityInfo`) + resolved account.
- `perms` — POSIX mode bits + xattr NAME list (`Llistxattr`); Windows DACL
  summary (ACE count + a compact per-trustee rights string, NOT an SDDL dump).
- `placeholder` — Windows cloud-recall (`FILE_ATTRIBUTE_RECALL_ON_*`/`OFFLINE`)
  detection without opening content; best-effort/absent elsewhere.

An unknown collector name is fail-soft: the rest run; the name is listed under
`summary.unknown_collectors`.

**Results**: NDJSON `{path, rel, <collector fields>}`. If the encoded NDJSON is
≤ 256 KiB it is INLINED in the command completion (`{summary, entries}`); larger,
it is gzipped and POSTed to `POST /api/v1/agents/{id}/inventory-results`
(header `X-Filearr-Command-Id`; 8 MiB cap; gzip-sniffed; write-if-absent →
`{config_dir}/inventory/<command_id>.ndjson.gz`) and the completion carries
`{summary, result_ref}`. The summary is ALWAYS present:
`{roots_expanded, entries, denied, placeholders_skipped, duration_ms,
collectors_run, collector_errors}` (+ diagnostics: `roots_truncated`,
`entries_capped`, `denied_sample`, `expand_errors`, `unknown_collectors`).

**Capability advertisement**: the agent attaches `capabilities:
{inventory_collectors: [...], inventory_version: 1}` to EVERY command poll;
central persists it VERBATIM on `agents.capabilities` (JSONB, migration
`c7d9e1f3a5b8`; size-capped by `FILEARR_AGENT_CAPABILITIES_MAX_BYTES`, an oversize
body is dropped, never a poll failure). **Follow-up (orchestrator):** expose
`agents.capabilities` in the agents LIST serializer (`api/agents.py`) so the UI
offers only supported collectors — deferred this round to avoid a concurrent-edit
conflict on that file; it is a one-line field add to the list `AgentOut`.

**W6-D2 → scan-root consumption seam**: at daemon start the agent resolves the
group `scan_selections` policy through the SAME `pathspec` engine and LOGS +
PERSISTS the effective roots to `{data_dir}/inventory/scan-roots.json` — it does
NOT start a scan from them (auto-start is a deliberate follow-up needing the scan
scheduler/cancellation coordination).

## 12. Docker container (Unraid-first, any container host)

The agent ships as a standalone container image — `ghcr.io/pwsh/filearr-agent`,
built from `agent/Dockerfile`, published by the same CI-gated `build`
workflow as the main image. This is the supported way to run an inventory
agent on Unraid (the native `.plg` plugin is deliberately deferred — see
`docs/research/unraid-agent-plugin.md`: a plugin's only unique power, running
while the array is stopped, buys a scanner nothing).

### 12.1 What the container does

The entrypoint (`agent/docker/entrypoint.sh`) composes the documented
"scan + run side by side" operating model:

1. **First start: enroll.** With `FILEARR_AGENT_CENTRAL_URL` +
   `FILEARR_AGENT_TOKEN` set, it runs `filearr-agent enroll`, retrying every
   30 s while central is unreachable (a genuinely bad/expired token keeps
   failing — read the container log, mint a fresh one). The token is
   single-use; after success the identity (key, cert, `state.json`) lives in
   `/config` and every later start skips enrollment entirely. Clearing the
   token variable afterwards is safe and tidy.
2. **`filearr-agent run`** — the replication daemon (outbox drain, cert
   renewal + rebind, policy poller, command poller, thumbnailer).
3. **Interval rescans** — `filearr-agent scan` over
   `FILEARR_AGENT_SCAN_ROOTS` every `FILEARR_AGENT_SCAN_INTERVAL` (default
   6 h). Interval, not watch: Unraid's `/mnt/user` is a FUSE (shfs) mount
   where inotify is unreliable — the same gotcha central documents for
   SMB/NFS. Rescans are mtime+size cheap; unchanged files cost a stat.

`SIGTERM` (`docker stop`) unwinds both processes cleanly. Logs go to the
container log (no `log_dir` set); `FILEARR_AGENT_LOG_LEVEL` tunes verbosity.

### 12.2 Environment reference

| Variable | Default | Meaning |
|---|---|---|
| `FILEARR_AGENT_CENTRAL_URL` | — (required on first start) | central base URL |
| `FILEARR_AGENT_TOKEN` | — (required on first start) | single-use enrollment token |
| `FILEARR_AGENT_NAME` | container hostname | name shown in the Agents console |
| `FILEARR_AGENT_SCAN_ROOTS` | — | comma-separated dirs to inventory |
| `FILEARR_AGENT_SCAN_INTERVAL` | `6h` | rescan cadence (Go duration) |
| `FILEARR_AGENT_SCAN_ON_START` | `true` | scan immediately at start |
| `FILEARR_AGENT_SHARE_MAP` | — | static share locations per root: comma-separated `localpath=location` pairs (`smb://host/share[/sub]`, `\\host\share`, `nfs://host/export`). Longest local prefix wins; overrides discovered exports. This is how a container — which can see no smb.conf and whose hostname is not the NAS's — still attaches share hints central renders as network-open links. The agent's web UI shows the resolved location per scan root (read-only when it comes from this variable, which outranks a mapping saved locally in `local-settings.json`) and lists malformed pairs verbatim as skipped |
| `FILEARR_AGENT_WEBUI_ALLOW_REMOTE` | `false` (`true` in the Unraid template) | opt-in NON-loopback web-UI bind (default `0.0.0.0:8686` when set without an explicit addr) so a Docker port mapping can reach it. The central policy gate (`web_ui_enabled`) + auth gate + read-only surface still apply; the loopback Host allow-list (a loopback-only rebinding defence) is skipped |
| `FILEARR_AGENT_SELF_UPDATE` | `false` in the image | `false` disables the self-update subsystem entirely (no boot check, no poll, no unpinned-key warning) — an immutable image updates by being pulled |
| `FILEARR_AGENT_LOG_LEVEL` | `info` | error\|warn\|info\|verbose\|debug (also gates the per-batch scan progress lines) |
| `FILEARR_AGENT_HASH_TIMEOUT_SECONDS` | `300` | Wall-clock budget per file for hashing. A corrupt/locked file on FUSE/SMB/NFS can block `read(2)` forever and freeze the whole walk at the same file every scan; past the budget the file is left unhashed and a WARN logs its path. `0` disables the bound |
| `FILEARR_AGENT_LOG_DIR` | `/config/logs` in the image | Per-process rotating log files (daemon / scan / entrypoint each write their own; 10 MiB x 5 rotation). The web UI Logs tab merges them into one timestamped view. Empty = stderr only (native installs' default) |
| `FILEARR_AGENT_LOG_STDERR` | `true` in the image | Keep echoing to stderr alongside the file sink even off-tty, so `docker logs` still carries every line |
| `FILEARR_AGENT_CA_BUNDLE` | — | PEM of extra trusted server roots (private CA) |
| `PUID` / `PGID` | `99` / `100` | uid/gid the agent runs as (Unraid nobody/users) |
| `TZ` | `Etc/UTC` | log timestamps |

All other `FILEARR_AGENT_*` knobs (§3 ffmpeg path, thumbnail tuning, poll
intervals) pass straight through — the entrypoint adds nothing between the
env and the binary. `FILEARR_AGENT_DATA_DIR` is pinned to `/config`.

**Root-list caveat:** `-root` flags MERGE into `/config/scan.json` (they never
remove). To retire a root, stop the container, delete it from `scan.json`'s
`roots` array, start again. Central config-group settings (presets, globs,
category gates) overlay `scan.json` per §9 exactly as on a host install.

### 12.3 Unraid setup (Community Applications template)

`unraid/filearr-agent.xml`. The template's shape, and why:

- **`/config` → `/mnt/user/appdata/filearr-agent`** (rw, cache pool):
  identity + SQLite index + outbox. Losing it is recoverable but costly — the
  agent would need re-enrollment and a full re-walk.
- **`/mnt/user` → `/mnt/user` read-only, 1:1.** Because container path ==
  host path, the paths central records ARE the real Unraid paths — no
  `library.native_prefix` mapping needed. If you narrow the mount to one
  share, keep it 1:1 (`/mnt/user/media:/mnt/user/media:ro`) to preserve that
  property; a non-1:1 mount works but then wants a `native_prefix` on the
  agent's library so share links resolve (the *arr remote-path-mapping
  pattern).
- **One optional port: 8686, the local web UI.** All *replication* traffic is
  outbound-only (mTLS to central + step-ca). The local read-only search UI
  binds loopback-only by default — unreachable through a Docker port
  mapping — so the template sets `FILEARR_AGENT_WEBUI_ALLOW_REMOTE=true` and
  maps 8686. Serving is still centrally gated: enable **Local web UI** under
  *Local surface* in a config group the container's agent belongs to (§9 — the
  **Global** group for the whole fleet, a higher-priority group for just these
  hosts; the key layers per key like everything else). Management stays
  in the central console; the local UI is READ-ONLY. Five tabs (2026-07-27):
  **Search** (category chips, sort, CSV/JSON export of results, full-path
  copy), **Filters** (condition-row builder compiling to the shared query
  grammar, live preview, open-in-search), **Reports** (local canned reports —
  categories, unmapped extensions, largest files, duplicates, future-dated —
  paged, CSV download), **Status** (agent version, per-root table: items,
  bytes, last scan time/duration/outcome, seen/new/changed), and **Logs**
  (merged multi-process view parsed into time/level/message/details columns,
  selectable depth, .log/CSV export).
- **Network `bridge`** when central runs elsewhere (set the full URL). If
  central's compose stack runs on the same box, use the shared custom network
  so `http://filearr:8000`-style names resolve.
- **PUID 99 / PGID 100** must be able to READ the scanned shares; the media
  mount being `ro` means the agent couldn't write even if compromised.

Steps: install the three template fields (central URL, token, roots) →
start → watch the log for `enrolled.` → the agent appears on the console's
Agents page → first scan replicates and central begins extraction.

### 12.4 Anywhere else (compose example)

```yaml
services:
  filearr-agent:
    image: ghcr.io/pwsh/filearr-agent:latest
    restart: unless-stopped
    environment:
      FILEARR_AGENT_CENTRAL_URL: https://filearr.example.com
      FILEARR_AGENT_TOKEN: "<paste single-use token>"   # remove after first start
      FILEARR_AGENT_NAME: nas-01
      FILEARR_AGENT_SCAN_ROOTS: /srv/media,/srv/documents
    volumes:
      - ./agent-data:/config
      - /srv/media:/srv/media:ro
      - /srv/documents:/srv/documents:ro
```

### 12.5 Updates and self-update

The image is built **without** a release-signing key pin, so the §8
self-update machinery is fail-closed (refuses every manifest) — intentional
for an immutable container. Updating = pulling the new image (Unraid's
built-in update flow); the identity and index in `/config` carry over
unchanged. Do not expect a container agent to take a signed §8 release, whatever
`auto_update` says; if you truly want in-container self-update, build with
`--build-arg UPDATE_PUBLIC_KEY=...` and ensure the restart policy relaunches
on exit code 20 (the service-managed swap handshake).

## Not yet wired (later phase-5 tasks)

- **P5-T4/T5**: replication (outbox → `replication-batch`) + reconciliation.
- ~~**P5-T6**: mTLS enforcement on the agent-plane endpoints.~~ **Shipped
  2026-07-17** — `FILEARR_AGENT_AUTH_MODE=mtls-header|both` (§6) + the
  `agents.<domain>` Caddy mTLS site + in-CT CA L4 passthrough (§7.4,
  `docs/ops/tls.md`). The interim cert-fingerprint bearer remains the default
  (`fingerprint` mode) until an operator flips the fleet over.
- **P10-T3/T4/T6/T13**: the consumers of `agent_commands` — the verify flow,
  the tus staging upload, the central download + SSE, and the RBAC transfer
  API. P10-T1 (this section) builds only the queue + its central surface.

## 13. In-daemon scan scheduling (service installs)

A lone `filearr-agent run` service is self-sufficient: the daemon runs scans
itself, so a native (Windows service / systemd) install needs **no external
Task Scheduler / cron job**. Motivation (live incident 2026-08-02): a Windows
agent's external scheduled-scan task did not survive a re-enroll — the daemon
heartbeated for nine days while its catalog silently froze at the old data.

The scheduler is **off until configured**. Knobs, per-key precedence
*merged `policy` section > merged `settings` section > env*:

| Source | Key | Meaning |
|---|---|---|
| group `policy` (§9) | `scan_cron` | 5-field cron, agent-local time |
| group `policy` | `scan_interval_seconds` | fixed cadence (≥300); `scan_cron` wins when both set |
| group `policy` | `scan_on_start` | one scan ~30 s after daemon start |
| group `settings` (§9) | `scan_schedule_cron` | same as `scan_cron`; a `policy` `scan_cron` overrides it |
| env | `FILEARR_AGENT_SCAN_CRON` / `FILEARR_AGENT_SCAN_EVERY` / `FILEARR_AGENT_SCAN_ON_BOOT` | local fallbacks for installs that don't author central config (`_EVERY` is a Go duration, e.g. `6h`) |

Example — schedule every enrolled agent nightly at 03:00 plus a catch-up scan
whenever the service (re)starts. Put it in **Global**, since it should apply to
machines no other group covers:

```bash
curl -s -X PATCH http://<ct-ip>:8484/api/v1/agents/config-groups/<global-id> \
  -H 'content-type: application/json' \
  -d '{"policy": {"scan_cron": "0 3 * * *", "scan_on_start": true},
       "note": "nightly fleet scan"}'
```

Remember `policy` REPLACES the section wholesale on write: send the whole
document you want Global to hold, not just the changed keys.

Semantics and safety:

- Scans run as a **child process** of the daemon (`filearr-agent scan` with
  the daemon's resolved `-data`/`-config`/`-log-dir`), so behavior, config
  precedence, and the per-command log file are identical to a hand-run scan,
  and a scan crash never takes the daemon down.
- The schedule re-resolves every tick from the cached policy: a policy edit
  applies within ~a minute of the next poll — no service restart.
- One scan at a time: a fire that lands while the previous scan is still
  running is skipped (logged), never queued or overlapped.
- **Containers are unaffected by default**: the Docker entrypoint keeps its
  own `FILEARR_AGENT_SCAN_INTERVAL` loop and the in-daemon scheduler stays
  off unless policy/env arms it — do not enable both, or you'll double-scan.
  The env names deliberately differ from the container's shell-loop vars.

## 14. Runbook: the quick_hash migration sweep (QH-T6, 2026-08-12)

**One-line summary.** Agents enrolled before 2026-07-18 hold wrong `quick_hash`
values for every stable file between 65,537 and 131,072 bytes; nothing repairs
them automatically; `POST /agents/{id}/rehash-sweep` is the repair.

### 14.1 What is broken and why nothing else fixes it

Before the QH-T1 fix (2026-07-18) both hashers — `extract.quick_hash` in Python
and `scan.QuickHash` in Go, kept byte-identical on purpose — read a fixed 64 KiB
head and appended a 64 KiB tail only when `size > 131072`. A file in
**65537..131072** therefore had its middle and tail silently unhashed. Two
different files sharing their first 64 KiB collided, which for structured
formats (JPEG/PNG/PDF/office containers) is routine. Symptoms: false duplicate
groups in the reports, and a mis-keyed tier-1 match in move detection.

Three facts make this need its own machinery rather than an existing sweep:

1. **`scan.diffEntry` will never revisit those files.** It re-hashes only when
   `size` or `mtime_ns` moved, or when `quick_hash` is empty. A stable file
   satisfies none of those, forever. That behaviour is deliberate and pinned by
   `TestScanNewChangedUnchangedMissingSelfHeal` (an unchanged rescan must cause
   zero `local_seq_no` churn), so the migration is its **own path**, never a
   flag on the scan path.
2. **Central's QH-T4 sweep cannot reach agent rows.** `worker.rehash_small_files`
   selects on `policy_version NOT LIKE 'cfg2:%'`, and `agentsync.apply_batch`
   **never writes `policy_version`** for agent-owned rows. So central cannot even
   identify a stale agent hash, let alone recompute one — it does not host the
   file. Central's own catalogue converged separately (`still_stale = 0`, checked
   2026-08-11).
3. **There is no hash-provenance column in the agent's SQLite index** and adding
   one would mean an `ALTER` on a million-row store on every deployed agent. The
   sweep's cursor (`rehash_state`, one singleton row) is therefore the only
   completion signal that exists anywhere.

Live scope when this shipped: **98,628 rows across seven libraries** —
video_media 37,095 · training 23,052 · audio 18,786 · pictures 14,641 ·
documents 3,351 · one Windows agent's `d:` 1,685 · tools 18. That is an upper
bound: anything created or modified since the agent picked up the QH-T1 binary
already self-corrected through the ordinary changed-file path, and the sweep
counts those as `verified`.

### 14.2 Enqueue

```bash
# Defaults: the defect band, unbounded, honour the idempotence short-circuit.
curl -sS -X POST -H "Authorization: Bearer $ADMIN_KEY" \
  https://filearr.example.com/api/v1/agents/$AGENT_ID/rehash-sweep

# Bounded first chunk, to time it before committing a night to it.
curl -sS -X POST -H "Authorization: Bearer $ADMIN_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"max_items": 5000}' \
  https://filearr.example.com/api/v1/agents/$AGENT_ID/rehash-sweep

# The SEPARATE, opt-in QH-T2 backfill: give the sub-band files an exact
# content_hash they never had. ~10x the reads, different benefit. Not the default.
curl -sS -X POST -H "Authorization: Bearer $ADMIN_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"min_size": 1, "max_size": 131072}' \
  https://filearr.example.com/api/v1/agents/$AGENT_ID/rehash-sweep
```

`admin` scope, behind `FILEARR_AGENTS_ENABLED` (404 when off). Body fields are
all optional: `force` (bool), `max_items` (1..10,000,000), `min_size` /
`max_size` (1..131,072, inclusive, `min <= max`). A bad value is a **422**, never
a silent clamp. **409** while a `rehash_sweep` is already `pending`/`picked_up`
for that agent — two runs would fight over the one cursor and double-emit the
rows they raced on.

The command carries `REHASH_TTL_SECONDS = 86_400`, not the 1h default, for the
same reason `reextract` does: `agentsync.sweep_decision` ranks `expires_at`
above the lease, so a faithfully heartbeating agent's multi-hour sweep would
otherwise be marked `expired` mid-run — the console's badge would clear and the
history would record a failure for work that completed. (No output is lost when
that happens; the events are already replicated and the cursor is durable. The
report would simply be a lie.)

### 14.3 Watching it

Three places, in increasing detail:

- **Agents table** — a `re-hashing` badge while the command is
  `pending`/`picked_up` (the page polls the command list every 15s).
- **row → details → About / versions → Hash migration** — the live counters,
  from the agent's health block. This is the authoritative view.
- **Admin → Agents → commands** — the terminal result map.

The health block (re-evaluated on **every** command poll, unlike capabilities,
which is why it rides health):

```json
{
  "rehash": {
    "fp": "h2-65537-131072",
    "started": "2026-08-12T09:00:00Z",
    "finished": "2026-08-12T14:31:07Z",
    "complete": true,
    "seen": 40000, "changed": 39100, "verified": 850,
    "skipped": 50, "failed": 0,
    "cursor": 812004, "min_size": 65537, "max_size": 131072
  }
}
```

Reading the counters:

- `changed` — rows actually corrected and emitted.
- `verified` — rows re-read and found already right. **Deliberately separate
  from `changed`.** High `verified` with low `changed` means an agent that
  ordinary rescans had already converged; summed into one number that is
  indistinguishable from a sweep that did nothing.
- `skipped` — size/mtime drifted, file vanished, or it is no longer a regular
  file. The ordinary scan owns all three cases.
- `failed` — the hash could not be computed (unreadable file, or the per-file
  `FILEARR_AGENT_HASH_TIMEOUT_SECONDS` bound fired on a hung mount). **The stored
  hash is left intact, never blanked** — writing an empty string would destroy a
  merely-suspect value and then read as a null-hash row the scan self-heals. The
  paths are WARNed in the agent log.
- `fp` — `h<scheme>-<min>-<max>`, from `scan.HashSchemeVersion` and the band.
  Human-readable so two agents can be compared by eye.

### 14.4 Idempotence, resume, and force

Keyed entirely on `fp`:

| stored state | request | outcome |
|---|---|---|
| same `fp`, `finished_at` set | plain | no-op, `reason: "already re-hashed at this scheme and size band"` |
| same `fp`, `finished_at` empty | plain | resume from `cursor_rowid` |
| different `fp` (band changed, or `HashSchemeVersion` bumped) | plain | cursor resets to 0, counters zeroed |
| any | `force: true` | cursor resets to 0 |

`force` is safe to press even on a converged agent: the sweep emits only on
change, so a forced run over correct rows counts them `verified` and writes
nothing. Candidate ordering is by SQLite `rowid`, which is assigned on insert and
never reshuffled — a scan running concurrently appends **above** the cursor, so
the walk can neither skip nor double-visit.

### 14.5 Blast radius (why this is not a re-index of the fleet)

- The events carry **no `extracted` payload**. `agentsync.apply_batch` merges
  `metadata_` only when `extracted is not None`, so a hash correction cannot
  cascade into re-extraction. This is load-bearing: at ~99k rows it is the
  difference between a hash fix and a fleet-wide re-parse.
- Emit-only-on-change keeps the *applied batch* count proportional to real
  corrections, which matters because every applied batch defers a Meilisearch
  sync job (`api/agent_commands.py`, the `defer_index_sync` at the end of the
  apply path).
- Move detection is agent-local, runs during the walk, and never runs on a
  replicated update — so a corrected hash arriving at central cannot trigger one.
- Invariant 2 holds: only the two identity hash columns move. `user_metadata` is
  untouched, and no file content leaves the agent.
- The sweep stops while the agent is suspended or central is in maintenance
  (`opState.ReplicationPaused`) — both because its events would only pile up, and
  because sustained I/O is exactly what "suspended" should stop.

### 14.6 Schema note (do not "fix" this)

`rehash_state` was added to `agent/internal/index/schema.go` **without** bumping
`schemaVersion`, and that is intentional. `integrity.go:schemaOutdated` treats a
version bump as *delete the store and rebuild from a fresh walk* — for an
additive, local-only, empty-on-create cursor table that would cost every deployed
agent a full re-walk and a re-emission of its entire index (~1.09M items on the
live agent) to gain twelve columns of bookkeeping. `CREATE TABLE IF NOT EXISTS`
in the DDL is applied in place by `migrate()` on the next open; `extract_state`
and `thumb_markers` took the same route for the same reason. Pinned by
`TestRehashStateIsAddedInPlaceWithoutLosingTheIndex`.

### 14.7 Rollback

`alembic downgrade b2e6d048f317` narrows the `agent_commands.kind` CHECK and
deletes any `rehash_sweep` rows first (Postgres validates a new CHECK against
existing rows). Nothing durable is lost: the sweep's progress lives in the
agent's own `rehash_state` cursor, so a re-upgrade and a re-issued command
resumes exactly where it stopped. What is lost is the console's history of the
run, not the run.
