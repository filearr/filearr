# Troubleshooting — unresponsive console / high CPU

Runbook for the failure mode where the web console stops responding (often
mid-deploy, during the "quiesce jobs" step) and the container shows high CPU.
Work top-down: each layer's result tells you which layer to open next.

## 1. Triage from any LAN machine (no host access needed)

```bash
ping -c 3 <ct-ip>                                   # is the CT kernel alive?
curl -sk -m 5 -o /dev/null \
  -w 'https 443: %{http_code} connect=%{time_connect}s total=%{time_total}s\n' \
  https://<ct-ip>/api/v1/version
curl -s -m 5 -o /dev/null \
  -w 'app 8484:  %{http_code} connect=%{time_connect}s total=%{time_total}s\n' \
  http://<ct-ip>:8484/api/v1/version
```

Interpretation matrix:

| Observation | Meaning |
|---|---|
| ping fails | CT is down or host networking broken → check the Proxmox host first (`pct status`, host load). |
| ping OK, port **connects** (`time_connect` ~0.001s) but no HTTP response | The listener exists (kernel/docker-proxy accepted the SYN) but the process behind it is wedged or CPU/memory-starved. Classic starvation signature. |
| ping OK, port does **not** connect (`time_connect` = 0, timeout) | The listener is *gone* — that service's process crashed or was OOM-killed. |
| 443 dead but 8484 half-alive (accepts, no response) | Caddy was killed (OOM killer preferentially shoots it — large-RSS neighbors like Meilisearch cause the pressure, but the killer's victim choice varies) while the app is starved but alive. Strong OOM-pressure signal. |

## 2. On the Proxmox host

```bash
pct status <vmid>
pct exec <vmid> -- uptime          # load average vs core count
pct exec <vmid> -- free -h         # is swap exhausted? (default CT: 512M swap)
pct exec <vmid> -- top -bn1 -o %CPU | head -25   # who is burning CPU
pct exec <vmid> -- top -bn1 -o %MEM | head -25   # who is holding memory
```

**OOM check — run on the HOST, not in the CT** (an unprivileged CT cannot
read the kernel log; cgroup OOM kills for the CT appear in the host journal):

```bash
journalctl -k --since '1 hour ago' | grep -iE 'oom|killed process'
dmesg -T | grep -iE 'oom|killed process' | tail -20
```

An `oom-kill` line naming a CT process (caddy, meilisearch, python, postgres)
confirms memory exhaustion. Load average far above core count with high `%sy`
or `kswapd` near the top of `top` means swap-thrash — same disease, slower
death.

If `pct exec` itself hangs (CT too starved to fork), skip to the recovery
ladder: `pct reboot <vmid>` is safe (see §5).

## 3. Inside the CT — which service

```bash
pct exec <vmid> -- docker stats --no-stream        # per-container CPU/mem NOW
pct exec <vmid> -- bash -c 'cd /opt/filearr && docker compose ps'
pct exec <vmid> -- bash -c 'cd /opt/filearr && docker compose logs --tail 50 app worker meilisearch caddy'
```

Per-service deep checks, by suspect:

- **Meilisearch** (most common CPU/RAM hog at ≥1M items — indexing spikes when
  a scan wrap-up or `index_sync` flushes a large document batch):

  ```bash
  pct exec <vmid> -- bash -c 'source /opt/filearr/.env 2>/dev/null;
    curl -s -H "Authorization: Bearer $FILEARR_MEILI_KEY" \
      "http://localhost:7700/tasks?statuses=enqueued,processing&limit=5"'
  ```

  A `processing` task with a six-figure document count explains a multi-minute
  CPU peg. Meilisearch indexing cannot be paused — either wait it out or
  restart the container (the task resumes/requeues; worst case the index is
  disposable and `rebuild_index` regenerates it from Postgres).

- **Postgres** — long-running/stuck queries:

  ```bash
  pct exec <vmid> -- bash -c 'cd /opt/filearr && docker compose exec -T postgres \
    psql -U filearr -c "SELECT pid, now()-query_start AS dur, state, wait_event_type,
    left(query,90) FROM pg_stat_activity WHERE state <> '"'"'idle'"'"'
    ORDER BY dur DESC LIMIT 10;"'
  ```

- **app** — the API tier is a single-worker uvicorn: it does not need to be
  the CPU hog to hang; any neighbor pegging the CT starves its event loop.
  If `docker stats` shows app near-idle while the box is pegged, the app is a
  *victim*, not the cause.

- **worker** — a scan in wrap-up ("stopping" status) is doing bounded work
  (final batch commit + progress publish); it should finish in seconds-to-a-
  couple-minutes. The deploy's quiesce step waits at most 180 s and then
  proceeds — that is by design and crash-safe.

## 4. Deploy-specific context ("stopping jobs" step)

During `step "quiesce jobs"` the OLD stack is still fully up — the deploy has
not touched containers yet. So a console that dies at this step was killed by
load, not by the container swap. The usual chain at scale:

1. `POST /scans/<id>/stop` → worker wrap-up commits the final batch →
   `index_sync` flushes deferred documents → Meilisearch starts a large
   indexing task.
2. Meilisearch RAM+CPU spike inside a small CT (default 4 cores / 4 GiB /
   512 MB swap) → OOM killer takes the largest expendable process and/or the
   box swap-thrashes.
3. Caddy dies (443 stops accepting), uvicorn starves (8484 accepts but never
   responds) — exactly the §1 matrix.

The build phase (`step "build + start stack"`) is the other classic CPU peg:
a base-image bump forces a full no-cache rebuild in the CT. That one is
expected and self-limiting — check `pct exec <vmid> -- docker ps` for a
buildkit container before assuming a hang.

## 5. Recovery ladder (least destructive first)

Safety invariants that make every rung safe: scans never hard-delete and a
crashed scan is marked `failed` on next startup; Postgres is crash-safe; the
Meilisearch index is a disposable projection (`rebuild_index`).

1. **Wait 5–10 minutes** if a Meilisearch indexing task or image build is the
   confirmed hog — both are finite.
2. **Restart the victims only:**
   `docker compose restart caddy app` (inside the CT app dir).
3. **Restart the hog:** `docker compose restart meilisearch` — interrupted
   indexing is recoverable; if search returns partial data afterwards, run
   the `rebuild_index` task from Admin.
4. **Full stack bounce:** `docker compose down && docker compose up -d`.
5. **CT reboot from the host:** `pct reboot <vmid>` — for when the CT is too
   starved to exec into.
6. **Rerun the deploy script** — it is idempotent; a rerun re-pushes source,
   rebuilds if needed, re-applies migrations, and re-triggers the scans it
   quiesced.

## 6. Prevention

- **Size the CT for the catalog.** 4 GiB is comfortable to roughly the
  low-hundreds-of-thousands of items; at ≥1M items give the CT 8 GiB+ and
  2 GiB swap (`pct set <vmid> --memory 8192 --swap 2048`, then restart the
  CT). Meilisearch's working set grows with the index and it spikes hard
  during indexing.
- **Cap the noisy neighbor** so an indexing spike degrades search instead of
  killing the box: add a `mem_limit` to the `meilisearch` service in the
  compose file sized to leave ≥1.5 GiB for Postgres+app+worker+Caddy.
- **Deploy off-peak or after scans finish** — the quiesce step exists so the
  wrap-up flush happens *before* containers are replaced; on a big catalog
  that flush is the load spike, so expect a busy minute right at the start
  of a redeploy.

## 7. Agents look down too?

An agent web UI going unreachable at the same time is usually coincidence or
a shared cause on the agent's own host (container update in progress, its own
resource pressure) — agents are independent of central. Check on the agent
host: `docker ps -a --filter name=filearr-agent`, then
`docker logs --tail 50 filearr-agent`. A freshly force-updated agent rebuilds
its SQLite indexes on first start (up to a few minutes of silence at
million-row scale) — that pause is expected.
