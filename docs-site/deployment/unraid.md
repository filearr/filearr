# Unraid

Filearr ships Community-Applications-format templates (`Container version="2"`),
one per container. Until they are published to Community Applications, install
them manually — the mechanics are [below](#installing-a-template).

This page is written as a **fresh install, end to end**: from an empty Unraid box
to an enrolled agent replicating into a catalogue you can search. Read it in
order the first time; the [field reference](#field-reference) at the end is what
you come back to.

## Pick a tier first

The stack is modular. Decide how far you need to go *before* you install
anything, because the tier determines how many containers you create and what
you have to have ready.

| Tier | Containers | Agent authentication | You need |
|---|---|---|---|
| **Simple** | `filearr` + `filearr-postgres` + `filearr-meilisearch` | `fingerprint` | Nothing but the box |
| **Proxied** | the three above, behind a reverse proxy you already run | `fingerprint` | Your existing SWAG / NPM / Unraid proxy |
| **Full parity** | the three above + `filearr-stepca` + `filearr-caddy` | `mtls-header` or `both` | A domain, a Cloudflare-hosted DNS zone, a spare LAN IP |

Three containers is the floor and it is a real floor: **Postgres and Meilisearch
are not optional**, they are where the catalogue and the search index live. What
*was* a fourth container — a separate `filearr-worker` — was folded into
`filearr` on 2026-08-12; see [upgrading from the two-container
layout](#upgrading-from-two-containers) if you already run it.

### Why not one container {#why-not-one-container}

Three containers is more install friction than one, and on Unraid — where an app
is a template you click — that friction is real. Bundling the database is a
deliberate *no*, argued for a **homelab**, not borrowed from enterprise practice:

1. **A bundled Postgres welds its major version to the app image.** The day that
   pin moves from 18 to 19, every existing data directory needs `pg_upgrade` run
   with *both* majors present — which a single image that ships exactly one major
   cannot do. Separate containers mean **you** choose when Postgres moves, and
   you can stay on 18 through as many Filearr releases as you like.
2. **Updating Filearr does not cycle your database.** Pull the app image, restart
   one container, done. Postgres and Meilisearch keep running with their caches
   warm.
3. **Memory isolation.** Meilisearch's indexing flush can spike past 6 GiB on a
   large catalogue (see [Console unresponsive, host CPU
   pegged](../operations.md#console-unresponsive-high-cpu)). In its own container
   that spike kills Meilisearch alone, and the index is rebuildable. In a shared
   container it takes the database down with it.
4. **Per-container logs and health are Unraid's primary debugging affordance.**
   "Which one is unhealthy" is a glance at the Docker tab.

The app and the worker had none of those properties — same image, same
environment, same volumes, differing only in the command — so merging *those two*
cost nothing and removed a standing drift risk, where a variable set on one
container and forgotten on the other produced behaviour nobody could explain.

## Before you start

- **A cache pool.** Postgres, Meilisearch and the CA all keep their data on
  `/mnt/cache/appdata/...`, not `/mnt/user/appdata/...`. Same share, same files —
  but `/mnt/user` goes through the shfs FUSE layer, whose file locking and mmap
  behaviour are unreliable, and that is the classic Unraid cause of `database is
  locked` stalls and index corruption. On Unraid 6.12+ a cache-only *exclusive*
  appdata share makes the two paths equivalent; the `/mnt/cache` default is
  simply correct everywhere.
- **Generated secrets.** Open the Unraid terminal and produce them now, into
  your password manager:

    ```bash
    openssl rand -hex 24   # POSTGRES_PASSWORD
    openssl rand -hex 24   # MEILI_MASTER_KEY
    openssl rand -hex 32   # FILEARR_SECRET_KEY        (never rotate this one)
    openssl rand -hex 32   # FILEARR_PROXY_SHARED_SECRET (full parity only)
    ```

    `FILEARR_SECRET_KEY` deserves special attention: it is the envelope key for
    alert-channel credentials and it is **not inside a database dump**. Restore a
    dump under a different key and everything reports success while every stored
    SMTP password and webhook secret becomes permanently undecryptable, silently.

- **Full parity only:** a domain whose DNS is hosted at Cloudflare, a Cloudflare
  API token scoped `Zone:DNS:Edit` on that zone (not a Global API Key), and a
  free IP address on your LAN for the proxy container.

## Step 0 — the shared Docker network

Container-name DNS does not work on Unraid's default bridge, and every DSN in
these templates addresses containers by name. Create the network once:

```bash
docker network create filearr
```

Everything except `filearr-caddy` sits on it. `filearr-caddy` is the exception
and it is deliberate — see [step 5](#step-5-caddy).

## Installing a template {#installing-a-template}

Fetch the XML files you need from the repo's
[`unraid/` folder](https://github.com/pwsh/filearr/tree/main/unraid):

```bash
cd /boot/config/plugins/dockerMan/templates-user/
for t in filearr filearr-postgres filearr-meilisearch filearr-stepca filearr-caddy; do
  wget -q "https://raw.githubusercontent.com/pwsh/filearr/main/unraid/$t.xml"
done
```

Then, in the Docker tab, **Add Container** → pick the template from the dropdown
→ fill the fields → **Apply**.

!!! danger "Delete each pristine XML after the container is created"
    On Apply, Unraid writes its own `my-<name>.xml` into the same directory. If
    the file you downloaded is still sitting there, **two** templates claim the
    same container name: the container's Edit page then loads the pristine
    *defaults* instead of your saved settings, and re-applying overwrites your
    saved copy with those defaults. One name, one file. `rm` each one as soon as
    its container exists.

## Step 1 — `filearr-postgres`

The source of truth for both the catalogue and the job queue. Install it first;
nothing else can start without it.

| Field | Required | Default | Notes |
|---|---|---|---|
| Data | required | `/mnt/cache/appdata/filearr-postgres` | Direct pool path. Never the array. |
| `POSTGRES_USER` | required | `filearr` | |
| `POSTGRES_PASSWORD` | **secret** | *(empty)* | The value you generated above. |
| `POSTGRES_DB` | required | `filearr` | |
| Port (optional) | optional | *(unmapped)* | Map only for `psql`/pgAdmin from the LAN. |

The template's Post Arguments carry memory tuning
(`shared_buffers=1GB`, `effective_cache_size=3GB`) mirrored from the compose
stack: the image default of 128 MB collapsed to a 56 % cache-hit ratio on a
million-item catalogue and starved the job queue. Shrink `shared_buffers` if the
box is small; `effective_cache_size` is planner *advice* and allocates nothing.

Start it and confirm the log ends with `database system is ready to accept
connections`.

## Step 2 — `filearr-meilisearch`

The search index. It is a **disposable projection** — everything in it is
rebuildable from Postgres — so it needs no backup, but it does need to exist.

| Field | Required | Default | Notes |
|---|---|---|---|
| Data | required | `/mnt/cache/appdata/filearr-meilisearch` | LMDB, mmap-based. Direct pool path, and keep it on fast storage. |
| `MEILI_MASTER_KEY` | **secret** | *(empty)* | Must match the app's Meilisearch Master Key. |
| `MEILI_NO_ANALYTICS` | optional | `true` | |
| `MEILI_UPGRADE_DB` | optional | `true` | Leave on. See below. |
| `MEILI_ENV` | optional | `production` | Hides the built-in preview UI. |
| Port (optional) | optional | *(unmapped)* | Debugging only. |

The pinned version is **1.53.0**. Two separate numbers matter and they are not
the same number:

- **1.48.2 is the security floor.** Below it, CVE-2026-57823 (tenant-token
  information disclosure) and CVE-2026-57824 (privilege escalation through an
  index-scoped key with broad actions) are unfixed, and this deployment sits in
  the named highest-risk shape for both — it issues tenant tokens and configures
  an embedder.
- **1.53.x is the tested baseline.** Filearr uses federated multi-search, `_geo`,
  `/similar`, embedders and `searchCutoffMs` with no version guards at all. On an
  older-but-still-patched server those features do not degrade; they error.

`MEILI_UPGRADE_DB=true` is not cosmetic either. A newer Meilisearch **refuses to
open an older database** and exits, so bumping the pin without it crash-loops the
container and takes search down. With it, the database migrates in place on first
start, and it is a no-op once current — which is why it is safe to leave on
permanently rather than being a one-shot you have to remember. The migration is
not atomic; back up the appdata path before moving the pin.

## Step 3 — `filearr`

The app: web UI, API, **and** the background worker (scans, extraction, hashing,
index sync, purge). One container, because its Post Arguments say `all`.

| Field | Required | Default | Notes |
|---|---|---|---|
| WebUI Port | required | `8484` | Container port 8000. |
| Config | required | `/mnt/user/appdata/filearr` | Thumbnails, caches, exports. Lock-insensitive, so the FUSE path is fine here. |
| Media | required | `/mnt/user/data/media` (ro) | Read-only. Library paths you create later are *in-container* paths under this mapping. |
| Database URL | required | `postgresql+psycopg://filearr:…@filearr-postgres:5432/filearr` | Password must match step 1. |
| Procrastinate DSN | required | `postgresql://filearr:…@filearr-postgres:5432/filearr` | Same database, no driver prefix. |
| Meilisearch URL | required | `http://filearr-meilisearch:7700` | |
| Meilisearch Master Key | **secret** | *(empty)* | Must match step 2. |
| Auth Enabled | optional | `true` | Session cookies are `Secure`; see [HTTPS](#https-on-unraid). |
| Secret Key | **secret**, optional | *(empty)* | Needed once you use alert channels. Never rotate. |
| Public Base URL | optional | *(derived)* | Set when the request-derived URL is wrong. |
| Recycle Retention Days | optional | `30` | |
| Worker Concurrency | optional | `4` | Parallel background jobs. |
| Worker Queues | optional | *(empty = all)* | Only set with a second, extract-only container. |
| Stop Grace (seconds) | optional | `60` | See the note below. |
| Postgres Data (disk monitor) | optional | `/mnt/cache/appdata/filearr-postgres` (ro) | Point at step 1's Data path. |
| Postgres Disk Watch | optional | `/pgdata` | Leave as-is. |
| Semantic Search | optional | `false` | Downloads and runs a local ONNX embedding model. |
| Content Sniffing | optional | `false` | libmagic MIME reclassification of extensionless files. |
| Auto Update Check | optional | `false` | The only automatic outbound call the product makes. |
| Thumbnail Budget (GiB) | optional | `5` | Advisory; nothing is ever deleted. |
| Log Recorder | optional | `true` | Feeds the console's Logs panel. |
| Distributed Agents | optional | `false` | Master switch. Turn on in [step 6](#step-6-agents). |
| Agent Auth Mode | optional | `fingerprint` | `mtls-header` / `both` for full parity. |
| Proxy Shared Secret | **secret**, optional | *(empty)* | Full parity only; see [step 5](#step-5-caddy). |
| CA URL / CA Root Fingerprint / CA Provisioner | optional | — / — / `filearr-agents` | Full parity only; see [step 4](#step-4-stepca). |
| CA Provisioner JWK | **secret**, optional | *(empty)* | Full parity only. |
| PUID / PGID / TZ | optional | `99` / `100` / `Etc/UTC` | |

!!! note "The `--stop-timeout=60` in Extra Parameters is load-bearing"
    Docker sends `SIGTERM` to the container, waits, then `SIGKILL`s — and the
    wait defaults to **10 seconds**. The merged entrypoint forwards `SIGTERM` to
    both the API and the worker and then holds the door open for
    `FILEARR_STOP_GRACE_SECONDS` (60) so the worker can finish jobs already in
    flight; the compose deployment carries the same 60 s as `stop_grace_period`
    because the 10 s default regularly cut jobs off mid-transaction during
    redeploys. Without the flag, Docker kills the container at 10 s and the
    in-container grace never gets a chance to matter. If you change one number,
    change both, and keep `FILEARR_STOP_GRACE_SECONDS` ≤ the stop timeout.

**First start bootstraps the database itself** — an idempotent
`scripts/init_db.py`, retried while Postgres is still coming up, so there is no
console step and no ordering race if the box reboots and brings everything up in
parallel. `FILEARR_AUTO_INIT_DB=false` opts out if you would rather run
migrations by hand.

The bootstrap runs **once**, before either child process starts. That matters:
Procrastinate's schema installer is not idempotent across concurrent processes,
which is exactly why the two-container layout could only ever let the *app*
migrate.

### First run

Open `http://<tower>:8484`.

1. **Create the admin account.** With `FILEARR_AUTH_ENABLED=true` the first visit
   shows a one-time bootstrap screen. It is one-time in the strict sense — once
   an admin exists, the endpoint is closed.
2. **Create your first library.** Admin → Libraries → New. The path is the
   **in-container** path: if you mapped `/mnt/user/data/media` to `/data/media`,
   a share at `/mnt/user/data/media/movies` is `/data/media/movies` here. The
   folder browser only offers paths inside the mapped roots, which is the fastest
   way to confirm you mapped what you think you mapped.
3. **Scan it.** Admin → the library → Scan.

A first scan looks like this, and knowing the shape saves you from stopping it
early:

- The **walk** runs first and is fast — a directory tree walk plus a `stat` per
  file. Progress publishes in batches of 250 files. A local array does tens of
  thousands of files a minute; a network mount is slower and that is the mount,
  not Filearr.
- Items appear in **search immediately**, with names and sizes but no depth:
  duration, resolution, codec, EXIF, page counts and hashes are all still
  missing.
- **Extraction** then runs as background jobs at negative priority, so scan and
  cancel controls always jump the queue. This is the long part. The Jobs page
  shows throughput; extraction backs off on its own if the database filesystem
  gets tight.
- **Nothing is ever deleted by a scan.** A file that has gone is tombstoned
  (`missing`), not removed, and purged later on the recycle-bin schedule.

Search something you know is there. If it comes back, the Simple tier is done —
stop here unless you want TLS or the agent fleet.

## Step 4 — `filearr-stepca` (full parity) {#step-4-stepca}

Only needed for `FILEARR_AGENT_AUTH_MODE=mtls-header` or `both`. This is the
private certificate authority that issues each agent its own client certificate.

| Field | Required | Default | Notes |
|---|---|---|---|
| Data | required | `/mnt/cache/appdata/filearr-stepca` | **CA private key material.** |
| CA Port | optional | *(unmapped)* | Leave empty; agents reach it through Caddy. |
| CA Name | required | `Filearr Agents CA` | First boot only. |
| CA DNS Names | required | `localhost,filearr-stepca` | **Add your public CA hostname**, e.g. `localhost,filearr-stepca,ca.example.com`. First boot only. |
| Provisioner Name | required | `filearr-agents` | Must match the app's CA Provisioner. |
| Remote Management | optional | `true` | Keep on. |
| Init Password | **secret**, optional | *(auto-generated)* | Set it, or scrape it from the log. |

!!! warning "Two values are printed once, into the container log, and never again"
    The **root certificate fingerprint** and the **CA administrative password**.
    Read both out immediately:

    ```bash
    docker logs filearr-stepca 2>&1 | grep -iE 'fingerprint|password'
    ```

    The fingerprint can be recovered later
    (`docker exec filearr-stepca step certificate fingerprint /home/step/certs/root_ca.crt`).
    The password cannot.

Then do three things.

**1. Set the certificate lifetimes.** The first-boot provisioner is bare. Filearr
expects short-lived agent certificates with a bounded grace for agents that have
been offline:

```bash
docker exec filearr-stepca step ca provisioner update filearr-agents \
  --x509-min-dur=24h --x509-default-dur=48h --x509-max-dur=72h \
  --allow-renewal-after-expiry \
  --admin-subject=step --admin-provisioner=filearr-agents \
  --admin-password-file=/home/step/secrets/password \
  --ca-url https://localhost:9000 --root /home/step/certs/root_ca.crt
```

`--allow-renewal-after-expiry` is the difference between "the laptop was off for
a week" and "re-enrol the laptop".

**2. Extract the provisioner's private JWK.** This is what lets Filearr mint the
short-lived one-time token an agent uses to collect its own certificate. Without
it, registration still succeeds but the token comes back `null` and enrolment
cannot finish.

```bash
# The encrypted key is published by the CA's own provisioner list. The Data path
# you mapped in this template IS /home/step inside the container, so the file
# lands somewhere both sides can see.
docker exec filearr-stepca sh -c 'step ca provisioner list \
    --ca-url https://localhost:9000 --root /home/step/certs/root_ca.crt' \
  | grep -o '"encryptedKey": *"[^"]*"' | head -1 | cut -d'"' -f4 \
  > /mnt/cache/appdata/filearr-stepca/provisioner.jwe

# Decrypt it. Under Remote Management the password is the CA ADMINISTRATIVE
# password from the first-boot log — NOT /home/step/secrets/password, which is
# the CA key password. This trips people up constantly.
printf '%s' 'PASTE-THE-ADMIN-PASSWORD' > /mnt/cache/appdata/filearr-stepca/adminpw
chmod 600 /mnt/cache/appdata/filearr-stepca/adminpw
docker exec filearr-stepca sh -c \
  'step crypto jwe decrypt --password-file /home/step/adminpw < /home/step/provisioner.jwe'

# Clean up both temporary files. They are key material.
rm -f /mnt/cache/appdata/filearr-stepca/adminpw \
      /mnt/cache/appdata/filearr-stepca/provisioner.jwe
```

The output is a JSON object beginning `{"kty":"EC",…}` and containing a `"d"`
member. That whole object is the value of **CA Provisioner JWK** on the `filearr`
container. Treat it exactly as you treat `FILEARR_SECRET_KEY`.

**3. Fill in the app.** On the `filearr` container set:

- **CA URL** → `https://ca.example.com` (the name agents will use, published by
  Caddy in the next step)
- **CA Root Fingerprint** → the fingerprint from the log
- **CA Provisioner** → `filearr-agents`
- **CA Provisioner JWK** → the JSON object above

If enrolment later fails with a null token, or certificates will not renew,
[Operations → agent enrollment / CA
failures](../operations.md#agent-enrollment-ca-step-ca-failures) is the
troubleshooting reference.

## Step 5 — `filearr-caddy` (full parity) {#step-5-caddy}

The TLS reverse proxy: Caddy 2.11.4 built with the Cloudflare DNS-01 solver and
the layer-4 (raw TCP/SNI) app. The stock Caddy image has neither, which is why
this is a published image (`ghcr.io/pwsh/filearr-caddy`) rather than a
configuration note.

!!! tip "Give this container its own IP"
    Set **Network Type: Custom : br0** and assign a **fixed IP address** on your
    LAN. This is the expected Unraid pattern and it exists because the proxy wants
    ports 80 and 443 — which on a typical Unraid box are already taken by the web
    GUI or by a reverse proxy you already run. Its own IP sidesteps the collision
    entirely, and the three public names then point at *that* address rather than
    at the server.

    A container on `br0` can still reach the `filearr` network by name, because
    Unraid attaches it to both; if your setup does not, use the app container's
    IP in **App Upstream** instead of `filearr:8000`.

| Field | Required | Default | Notes |
|---|---|---|---|
| Profile | required | `internal` | `internal` or `acme`. |
| HTTPS Port | required | `443` | TCP only. Do not add UDP. |
| HTTP Port | optional | `80` | Redirects only; the certificate comes from DNS-01. |
| Compat HTTPS Port | optional | *(unmapped)* | Only for bookmarks pointed at the compose stack's 8443. |
| Certificates | required | `/mnt/cache/appdata/filearr-caddy` | Must persist. |
| Caddy State | optional | `/mnt/cache/appdata/filearr-caddy-config` | |
| step-ca Root (read-only) | optional | `/mnt/cache/appdata/filearr-stepca` (ro) | Same path as step 4's Data. `acme` profile only. |
| Domain | optional* | *(empty)* | `acme` only, and then required. The **apex zone**, e.g. `example.com`. |
| ACME Email | optional* | *(empty)* | `acme` only, and then required. |
| Cloudflare API Token | **secret**, optional* | *(empty)* | `acme` only, and then required. `Zone:DNS:Edit`, that zone only. |
| Proxy Shared Secret | **secret** | *(empty)* | Must equal the app's Proxy Shared Secret. |
| App Upstream | optional | `filearr:8000` | Container port 8000, not the 8484 host mapping. |
| CA Upstream | optional | `filearr-stepca:9000` | |

**Start on `internal`.** It uses Caddy's own self-signed CA, needs no domain, no
DNS credentials and no internet, and proves the proxy can reach the app before
any certificate machinery is involved. Browse to `https://<caddy-ip>/`, accept
the warning, confirm you get the Filearr console.

**Then switch Profile to `acme`.** Point three DNS records at the container's IP
first:

| Name | Serves |
|---|---|
| `filearr.example.com` | the web UI and API |
| `agents.example.com` | the agent plane — a client certificate is **required** here |
| `ca.example.com` | step-ca, passed through **raw** |

One wildcard certificate for `*.example.com` covers all three, obtained through
the Cloudflare DNS-01 challenge — so nothing on the internet has to reach port
80, and this works behind NAT. The `acme` profile refuses to start with a clear
message if Domain, ACME Email or the token is missing, rather than serving a
half-configured site.

Three things about this configuration are deliberate and worth not "fixing":

- **`ca.example.com` is not TLS-terminated.** Certificate renewal authenticates
  with the agent's own client certificate on the direct connection; an L7 proxy
  in the middle silently breaks it. The layer-4 listener peeks at the TLS
  ClientHello and raw-proxies that one hostname through.
- **HTTP/3 is off.** Caddy would otherwise advertise `Alt-Svc: h3`, but a
  published 443 mapping is TCP-only unless you say `/udp`, so browsers that honour
  the advertisement stall on a site that is otherwise perfectly healthy (a live
  incident, 2026-07-19). Do not publish UDP/443 to "fix" it either — QUIC would
  bypass the raw `ca.` passthrough above.
- **The DNS-01 self-check queries public resolvers explicitly.** If your LAN
  resolver overrides this zone, it answers from its local view, never sees the
  challenge record, and issuance times out while Cloudflare has already published
  it (a live incident, 2026-07-17).

If issuance fails, [Operations → TLS and ACME issuance
failures](../operations.md#tls-and-acme-issuance-failures) is the reference.

### Turning on `mtls-header`

Do this **after** at least one agent is enrolled and working, not before —
`mtls-header` fails closed by design, and flipping it early locks out the fleet
you have not built yet. The order that avoids downtime:

1. On `filearr`, set **Proxy Shared Secret** to the same value as the proxy's,
   and **Agent Auth Mode** to `both`. Apply.
2. Point each agent at `https://agents.example.com`. An enrolled agent presents
   its client certificate automatically.
3. Watch the Agents page. Each agent shows a **transport badge** that central
   observes for itself — it is not self-reported. Wait until every active agent
   reads `mTLS`.
4. Set **Agent Auth Mode** to `mtls-header`. Apply.

!!! warning "Enrolment always runs against the main URL"
    `agents.example.com` requires a client certificate, and an agent that has not
    enrolled yet does not have one. Enrol against `https://filearr.example.com`,
    *then* switch that agent to the mTLS URL. This is a one-way door in the wrong
    order and a two-second fix in the right one.

## Step 6 — the first agent {#step-6-agents}

An agent inventories a machine Filearr cannot see directly — another server, a
desktop, a NAS — and replicates the inventory in over mutual TLS. Central does
the heavy extraction after replication.

1. On `filearr`, set **Distributed Agents** to `true` and Apply. The agent API
   404s and the Agents page stays hidden until you do.
2. Console → **Agents** → **Mint enrollment token**. The raw token is shown
   **once** — only its hash is stored. It is single-use and expires in an hour by
   default.
3. Install the agent on the target machine and give it two things: the **central
   URL** (`https://filearr.example.com`) and the **token**. On another Unraid box
   that is the `filearr-agent.xml` template, which needs no Postgres, no
   Meilisearch and no network setup — just those two fields.
4. The agent generates its own keypair, gets its certificate directly from
   step-ca using a one-time token central minted, and reports its fingerprint
   back. Its private key never leaves the machine and central never sees a CSR.
5. It appears on the Agents page as `pending`, then flips to `active` on its own.

The full walkthrough, configuration groups, phased rollouts and the local
read-only UI are in [Distributed agents](../agents.md).

## Field reference {#field-reference}

Every field of every template is tabulated in its own step above — steps
[1](#step-1-filearr-postgres), [2](#step-2-filearr-meilisearch),
[3](#step-3-filearr), [4](#step-4-stepca) and [5](#step-5-caddy). Two
conventions run through all of them:

- Anything marked **secret** is `Mask="true"` in the template, so Unraid hides it
  in the UI and in screenshots. Fill it once; the value is stored in the
  container's saved template.
- Anything marked *optional* is pre-filled with a safe default and lives under
  **Advanced View**. The defaults are chosen so that a container started with
  nothing but the required fields is correct, just conservative — semantic search,
  content sniffing and the automatic update check are all off, and turning any of
  them on is a deliberate act.

## Using a Postgres or Meilisearch you already run {#existing-servers}

Both are ordinary servers and Filearr is happy to use yours. Three things are not
negotiable.

!!! danger "Postgres 18 or newer"
    Filearr uses PostgreSQL 18's native `uuidv7()` as the server-side default on
    34 primary keys. It is probed at migration time *and* at every boot, so an
    older server fails loudly rather than corrupting anything — but it does fail.
    Most homelab Postgres installs are 15 or 16. Check with `SELECT version();`
    before you point a DSN at one.

!!! danger "A dedicated DATABASE, not a dedicated schema"
    Filearr assumes it owns the whole database. There is no schema isolation, the
    table names are generic (`items`, `users`, `libraries`), the job queue's own
    tables share the schema, and the in-app backup's `pg_dump` has no `-n`/`-t`
    filter — so pointing Filearr at a shared database means your backups quietly
    contain the other application's data too, and a `--clean` restore is a live
    grenade. A dedicated schema is *not* sufficient.

    No superuser is needed: ordinary `CREATE` rights on its own database are
    enough. `ltree` is used if present and falls back to text/btree if not.

!!! warning "Meilisearch: the key needs broad reach"
    Filearr creates and manages its own indexes and issues tenant tokens, so the
    key you give it is not narrowly scoped. Sharing an instance with other
    applications is safe in the sense that matters — Filearr only ever deletes
    indexes it owns, an ownership gate added deliberately — but understand that
    the credential you hand it is powerful.

    Run **1.53.x**. 1.48.2 is the security floor; 1.53.x is the only version this
    is tested against, and the gap between those two statements is described in
    [step 2](#step-2-filearr-meilisearch).

To use them, skip steps 1 and 2 and point **Database URL**, **Procrastinate
DSN**, **Meilisearch URL** and **Meilisearch Master Key** at your servers. If they
are not on the `filearr` Docker network, use host IPs rather than container names.

## HTTPS on Unraid {#https-on-unraid}

Three options, in ascending order of effort:

1. **A reverse proxy you already run** — SWAG, Nginx Proxy Manager, Unraid's
   built-in proxy. Real certificate, nothing new to trust, and it is the
   Unraid-native answer. Sufficient for everything except `mtls-header`.
2. **`filearr-caddy` on the `internal` profile** — self-signed, LAN-only, no
   domain required. HTTPS in about a minute, at the cost of trusting the
   generated root on each client.
3. **`filearr-caddy` on the `acme` profile** — a real wildcard certificate, plus
   the mTLS agent plane and the CA passthrough. This is the only path to
   `mtls-header`. See [step 5](#step-5-caddy).

!!! note "Set up HTTPS before you rely on logins"
    Auth is on by default and session cookies are marked `Secure`, so the login
    flow needs HTTPS anywhere beyond plain-HTTP LAN testing. Either put a proxy in
    front first, or set `FILEARR_AUTH_ENABLED=false` while you are evaluating on a
    trusted LAN.

## Coming from an existing deployment {#fresh-vs-migrate}

If you already run Filearr somewhere else — a compose stack, a Proxmox LXC — you
have a genuine choice, and it is not obvious which way to go.

**Migrating** carries everything across and is documented step by step in
[Operations → move an existing deployment to a new
host](../operations.md#migrate-to-a-new-host). It is more work and it has one
step people skip at their peril (remapping library roots).

**Starting fresh** is legitimate: re-scanning rebuilds the catalogue from the
filesystem, which is where the catalogue came from in the first place. But
re-scanning only recovers what is *derivable from disk*. These are not, and a
fresh install begins without them:

- `user_metadata` edits — every title, description or field you corrected by hand
- tags and custom field values
- saved searches
- alert rules, and the channel credentials encrypted under `FILEARR_SECRET_KEY`
- user accounts, API keys, and RBAC path grants
- configuration groups and their version history
- library definitions, include/exclude rules and scan schedules
- agent enrollments (every agent re-enrols against the new CA)
- frecency history — the "things you actually open rank higher" signal, which
  rebuilds itself over weeks of use rather than instantly

Thumbnails and the Meilisearch index are *not* on that list: both regenerate on
their own, the first lazily on view and the second from Postgres.

!!! tip "Take a bundle backup of the old deployment anyway"
    Even when the plan is a clean start. It is one command, it is the only way
    back if something on the old box turns out to have mattered, and it exercises
    the backup tooling end to end while you still have a working system to test
    against:

    ```bash
    scripts/backup.sh                       # dump + .env + step-ca + manifest
    scripts/verify-backup.sh backups/filearr-<timestamp>/
    ```

    `verify-backup.sh` restores the dump into a throwaway Postgres container,
    counts rows against the manifest, and compares the secret-key fingerprint. A
    backup you have never restored is a hypothesis; this makes it a fact. Keep the
    bundle until the new box has been running long enough that you would not go
    back.

## Upgrading from the two-container layout {#upgrading-from-two-containers}

Before 2026-08-12 the stack had a separate `filearr-worker` container running the
same image with a different command. It is gone: `filearr` now runs both
processes, selected by `all` in its Post Arguments.

To upgrade:

1. **Stop and remove `filearr-worker`.** Docker tab → the container → Remove. It
   has no volumes of its own that `filearr` does not also mount, so nothing is
   lost. Delete `my-filearr-worker.xml` from
   `/boot/config/plugins/dockerMan/templates-user/` too.
2. **Edit `filearr`.** Add `all` to **Post Arguments**, and add
   `--stop-timeout=60` to **Extra Parameters**. Apply.
3. Check the log. You should see one bootstrap, then a line naming both child
   processes.

Nothing in the database changes and no re-scan is needed. If you were running a
*second* worker for throughput, you can still do that — install `filearr` again
under a different name with empty Post Arguments and the worker command, or move
to the compose deployment, which keeps separate services and `--scale worker=N`
precisely for that case.

## Optional: hardware-accelerated video thumbnails

To use an Intel iGPU for video poster frames, add the render device and group to
the **`filearr`** container's Extra Parameters (this used to go on the worker):

```text
--device /dev/dri --group-add $(stat -c '%g' /dev/dri/renderD128)
```

Safe to skip — the thumbnail pipeline probes for the render node once and falls
back to software decoding with zero failed attempts when it is absent.

## Backup and restore {#backup-and-restore}

Everything else in the manual takes backups through `docker compose`, which
Unraid does not have. These are the native equivalents. Read the
[state inventory](../operations.md#state-inventory) first — it lists what exists
besides the database and what losing each piece costs.

### The things to back up

1. **The database** — `filearr-postgres`. Everything you cannot re-derive.
2. **`FILEARR_SECRET_KEY`** — set on the `filearr` container, and **not inside
   the dump**. Restore under a different key and everything reports success while
   every stored SMTP password, webhook secret and Apprise URL becomes permanently
   undecryptable, with no error anywhere. Copy it into your password manager the
   day you set it.
3. **`/mnt/cache/appdata/filearr-stepca`** — if you run the full-parity tier.
   This is CA private key material and it is the one thing in the stack that no
   amount of re-scanning can rebuild: a new CA invalidates every certificate it
   ever issued, and every agent has to re-enrol.
4. **`/mnt/user/appdata/filearr/agent-releases`** — only if you have uploaded a
   custom signed agent build. Everything else under `/config` (thumbnails,
   models, exports, inventory) rebuilds itself.

### Take a backup

From the Unraid terminal (or **Docker → filearr-postgres → Console**):

```bash
mkdir -p /mnt/user/backups/filearr
docker exec filearr-postgres pg_dump -U filearr -Fc filearr \
  > /mnt/user/backups/filearr/filearr-$(date -u +%Y%m%dT%H%M%SZ).dump
```

`-Fc` is the compressed custom format `pg_restore` reads. Note there is **no
`-T`** here: that flag is a `docker compose exec` requirement, and plain
`docker exec` does not allocate a TTY unless you ask for one — adding `-t` would
corrupt the binary dump.

For the CA, while it is stopped or accepting the small risk of a hot copy:

```bash
tar czf /mnt/user/backups/filearr/stepca-$(date -u +%Y%m%dT%H%M%SZ).tar.gz \
  -C /mnt/cache/appdata filearr-stepca
```

Write both to a share that is **not** on the same pool as `/mnt/cache/appdata`,
and have your existing off-box sync pick them up. A backup that dies with the
cache pool is not a backup.

### Schedule it (User Scripts plugin) {#scheduling-with-user-scripts}

Install **User Scripts** from Community Applications, then *Add New Script* →
name it `filearr-backup` → *Edit Script*:

```bash
#!/bin/bash
# Nightly Filearr backup. Keeps the newest 7 dumps.
set -euo pipefail
DEST=/mnt/user/backups/filearr
KEEP=7

mkdir -p "$DEST"
ts="$(date -u +%Y%m%dT%H%M%SZ)"
out="$DEST/filearr-$ts.dump"

# Refuse rather than write a truncated file if the database is not up.
docker exec filearr-postgres pg_isready -U filearr >/dev/null

# Write to .partial and rename, so a crash never leaves a half dump that the
# prune below would count as a good one.
docker exec filearr-postgres pg_dump -U filearr -Fc filearr > "$out.partial"
mv "$out.partial" "$out"
echo "wrote $out ($(du -h "$out" | cut -f1))"

# Prune to the newest $KEEP.
ls -1t "$DEST"/filearr-*.dump | tail -n +$((KEEP + 1)) | while read -r old; do
  echo "removing $old"; rm -f "$old"
done
```

Set the schedule to **Scheduled Daily** (or *Custom* with a cron expression such
as `30 3 * * *`). Use *Run Script* once by hand and read the output before you
trust the schedule.

!!! warning "Record the secret key alongside the dumps"
    The script above backs up the database and nothing else, because that is all
    a shell on the Unraid host can reach without stopping containers. Store
    `FILEARR_SECRET_KEY` (and `POSTGRES_PASSWORD`, `MEILI_MASTER_KEY`,
    `FILEARR_PROXY_SHARED_SECRET`, `FILEARR_CA_PROVISIONER_JWK`) somewhere safe
    **now**. The About page shows the secret key's *fingerprint*, which lets you
    check a key but never recover one.

### How the "Backup/Restore Appdata" plugin differs

The Community Applications **Backup/Restore Appdata** plugin is a valid backup
model, but a *different* one — and mixing the two up is how people end up with a
corrupt database in an archive:

- It is a **cold copy**: it stops the containers, tars the appdata paths, and
  starts them again. That is safe precisely *because* Postgres is stopped.
- `pg_dump` is a **live logical** backup: consistent by construction, restorable
  into a different Postgres major, and it takes no downtime.

Either works. If you use the plugin, **check that it sweeps both locations** —
the Filearr stack deliberately splits them:

| Container | Appdata path | Why |
|---|---|---|
| `filearr` | `/mnt/user/appdata/filearr` | `/config` is lock-insensitive, so the FUSE path is fine |
| `filearr-postgres`, `filearr-meilisearch`, `filearr-stepca` | `/mnt/cache/appdata/...` | direct pool path — `/mnt/user`'s shfs layer has unreliable file locking/mmap, the classic Unraid cause of database corruption |

A plugin configuration that only sweeps `/mnt/user/appdata` therefore backs up
the thumbnails and misses the database entirely. Add both, or use `pg_dump` for
the database and let the plugin handle the rest.

### Restore

Follow the order. Steps 1 and 2 are not optional.

```bash
# 1. VERIFY FIRST — restore into a throwaway container and count rows. Do this
#    before you touch the live stack.
docker run -d --rm --name pgverify \
  -e POSTGRES_USER=filearr -e POSTGRES_PASSWORD=verify -e POSTGRES_DB=filearr \
  postgres:18.4
sleep 10
docker exec -i pgverify pg_restore -U filearr -d filearr --no-owner --clean --if-exists \
  < /mnt/user/backups/filearr/filearr-YYYYmmddTHHMMSSZ.dump
docker exec pgverify psql -U filearr -d filearr -tAc 'SELECT count(*) FROM items'
docker exec pgverify psql -U filearr -d filearr -tAc \
  "SELECT value FROM instance_meta WHERE key = 'secret_key_fingerprint'"
docker rm -f pgverify
```

Compare that last value against `sha256(FILEARR_SECRET_KEY)` truncated to 16 hex
— it is the fingerprint the app shows on its About page. **If they differ, the
encrypted alert-channel secrets in this dump cannot be read by this deployment.**

```bash
# 2. SECRETS BEFORE DATA. On the `filearr` container set FILEARR_SECRET_KEY back
#    to its ORIGINAL value (Docker tab → Edit → Apply), along with
#    POSTGRES_PASSWORD, MEILI_MASTER_KEY and (full parity) the proxy secret and
#    CA provisioner JWK.

# 3. Stop the app so nothing writes during the load.
docker stop filearr

# 4. Load the dump.
docker exec -i filearr-postgres pg_restore -U filearr -d filearr \
  --clean --if-exists --no-owner < /mnt/user/backups/filearr/filearr-YYYYmmddTHHMMSSZ.dump

# 5. Start the app. It runs the idempotent scripts/init_db.py bootstrap itself,
#    which is the only thing that can bring an arbitrary prior schema to head.
docker start filearr

# 6. Rebuild the search index (Meilisearch was never backed up — it is a
#    projection). Also available as "Rebuild search index" on the Jobs page.
curl -X POST http://<tower-ip>:8484/api/v1/system/rebuild-index
```

Thumbnails regenerate lazily on first view. Finally, **open the console and check
the About page**: if `FILEARR_SECRET_KEY` does not match what the restored
database was encrypted under, you get a red row there, a banner on the Admin
dashboard, and an error in the log. A clean About page is what "the restore
worked" looks like.

### Without a shell: the in-app backup

The Jobs page has a **Back up now** button that writes a dump into `/config` and
offers it for download — no terminal needed. It is honest about its limits: a
container cannot read the host's environment or another container's volume, so it
cannot include `FILEARR_SECRET_KEY` or the CA. Treat it as a convenient database
snapshot, not as your whole backup. See
[in-app backup](../operations.md#in-app-backup).

## Where to next

- [Distributed agents](../agents.md) — the fleet, configuration groups, phased
  rollouts. This box can also *be* an agent for a central running elsewhere, via
  the `filearr-agent` template.
- [Operations & recovery](../operations.md) — every failure mode with a symptom
  you can search for.
- [Upgrades & migrations](upgrades.md) — read before moving a version pin.
- [Configuration reference](../reference/configuration.md) — every environment
  variable, not just the ones the templates surface.
