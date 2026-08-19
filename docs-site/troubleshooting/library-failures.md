# Why is a library failing?

The Libraries page shows three failure signals per library. Each has a different
cause class and a different fix. This page explains what they mean, how the
**Diagnose** dialog gathers everything into one report, and what to do about
every verdict it can produce.

!!! tip "Nothing on this page loses data"
    Scans **never hard-delete**. When a file cannot be seen — unmounted share,
    permission error, renamed folder — the item is *tombstoned* (`missing`),
    stays in Postgres, and **comes back on the next scan that sees it**.
    Tombstoned items are only purged after the recycle-bin retention
    (`FILEARR_RECYCLE_RETENTION_DAYS`, default 30 days). Fix the cause at
    your own pace; see [recycle-bin recovery](../operations.md#recycle-bin-tombstone-recovery).

## The three failure signals {#signals}

| Signal on the Libraries page | What it means | Cause class |
|---|---|---|
| **FAILED** badge on the last scan | The scan walk crashed and was marked `failed` (never left `running`). The library was *not* fully walked; nothing was tombstoned by that run. | Mount / permissions / worker crash |
| **Errors** column (count) | Items that were seen and indexed by name/path but whose *extraction* (ffprobe, exiftool, PDF text …) failed. Clicking the count opens the per-item list with an error kind per item. | Missing tool / corrupt file / safety ceiling / I/O |
| **Failed jobs** (Jobs page) | Background jobs referencing the library that exhausted their retries. Their recorded error text is the sharpest evidence for a crash. | Worker-side crash |

The **Diagnose** button on the library row runs one read-only pass over all
of it — a bounded probe of the root path from *inside* the container, the
recent scan runs, the extraction-error summary, failed jobs, the owning agent
(if any) and recent warning-level log lines — and turns the findings into
ordered **verdicts**.

## Running Diagnose and reading verdicts {#running-diagnose}

1. Libraries → library row → **Diagnose**. The dialog probes and renders in a few
   seconds; a hung mount is capped at 8 s and becomes a verdict itself.
2. **Verdicts** are listed first, worst first: `error` → `warning` → `info` →
   `ok`. Each card has a plain-language cause, a **What to do** list, an
   expandable **Evidence** block (the raw numbers the verdict was derived
   from) and a **Docs ↗** link that lands on the matching section below.
3. Below the verdicts, collapsible **raw sections** show the data itself —
   see [Reading the raw sections](#raw-sections).
4. **Copy report** puts the whole JSON on the clipboard; **Re-run** re-probes
   after you changed something.

The API is `GET /api/v1/libraries/{id}/diagnose` (read scope) if you want the
same report from a script.

## Verdicts, one by one {#verdicts}

### Agent-owned libraries

Central never opens the files of an agent-owned library — scans run *on* the
agent and central only receives results. Path verdicts are therefore skipped
for them; the agent's own health is what matters.

### agent-missing {#agent-missing}

**Meaning.** The library's `source_agent_id` points at an agent that no longer
exists (deleted). Nothing can scan or serve it.

**Confirm.** Agents does not list the agent named in the report.

**Fix.** Re-enrol the agent and re-add the library, or delete this library.
Deleting a library is the product's one intentional hard delete — items and
scan history go with it.

**Data at risk.** Nothing on disk is touched; only the catalogue rows go if
you delete the library.

### agent-revoked {#agent-revoked}

**Meaning.** The owning agent's certificate was revoked. A revoked agent
cannot report; scans and metadata for this library are frozen at their last
state.

**Confirm.** Agents shows the agent as revoked; its log shows
rejected polls.

**Fix.** Un-revoke or re-enrol under Agents (see
[Distributed agents](../agents.md)).

**Data at risk.** None — the catalogue keeps its last replicated state.

### agent-offline {#agent-offline}

**Meaning.** The agent has not contacted central for more than five minutes.
Its libraries look stale, not broken.

**Likely causes.** Agent service stopped or crashed; host rebooted; network or
TLS problem between agent and central.

**Confirm.** On the remote host: `systemctl status filearr-agent` (or the
Windows service) and its log — look for the policy/command polls.

**Fix.** Start the service; check it can reach central's agents endpoint. See
[agent enrollment / CA failures](../operations.md#agent-enrollment-ca-step-ca-failures).

**Data at risk.** None.

### agent-online {#agent-online}

**Meaning.** The owning agent is healthy. The path shown in the report is the
agent's, not this container's — central-side path verdicts do not apply.

### Path health

These apply to centrally-scanned libraries. The probe runs **inside the
`filearr` container** as the process user, so it sees exactly what a scan
would see.

### path-hung {#path-hung}

**Meaning.** Listing the root path did not return within 8 s. This is the
classic signature of a hung SMB/NFS/rclone mount inside the container.

**Likely causes.** The SMB/NFS server went away; an rclone/FUSE mount died
and left a stuck mount point; the container is holding a stale bind of a
remount it cannot see.

**Confirm.**

```bash
# from the host — does the same path hang here too?
timeout 10 ls -la /mnt/user/media | head
mount | grep -E 'cifs|nfs|fuse'
dmesg | tail -n 30                       # "CIFS VFS: ... server not responding" etc.
# from inside the container
docker exec filearr timeout 10 ls -la /data/media | head
```

**Fix.** Remount on the host (or restart the rclone mount service). Docker
bind mounts only see a *remount* when the compose file uses
`bind.propagation: rslave` for the media path — otherwise restart the
`filearr` and `filearr-worker` containers after remounting.

**Data at risk.** None. A scan that hits a hung mount either times out
(`failed`, nothing tombstoned) or sees an empty tree — which the
[empty-scan guard](#empty-scan-guard) also turns into a `failed` run with
nothing tombstoned.

### path-missing {#path-missing}

**Meaning.** The root path does not exist inside the container. The next
scan **fails before walking** (`assert_scannable_root`): nothing is tombstoned
and nothing is deleted; the run shows `failed` with the missing path in its
error.

**Likely causes.** The Docker volume mapping does not cover the directory;
the share was renamed or moved on the host; the library was created with a
host path instead of the container path.

**Confirm.**

```bash
docker exec filearr ls -la <root_path>          # No such file or directory?
docker inspect filearr --format '{{json .Mounts}}' | python3 -m json.tool
```

On Unraid: Docker tab → filearr → **Edit** → check the *Media* path mapping
(host path → container path) covers this directory.

**Fix.** Add or widen the mapping and restart the container. If the share was
renamed or moved, **edit the library's root path** (Edit → Root path) rather
than deleting and re-adding it — item identity is `(library, rel_path)`, so an
edited root keeps every item, its metadata edits and its history.

**Data at risk.** None while the recycle-bin retention has not elapsed. If
the path was missing for longer than the retention, tombstoned items were
purged and the next scan re-ingests the files as new (user metadata edits on
those items are gone).

### path-not-dir {#path-not-dir}

**Meaning.** The root path exists but is a file, or a broken mount point that
stats as something other than a directory.

**Confirm.** `docker exec filearr stat <root_path>`.

**Fix.** Fix the mount, or point the library at the directory above.

**Data at risk.** None.

### path-permission {#path-permission}

**Meaning.** The container user (PUID/PGID, default `99:100` on Unraid) cannot
read and traverse the directory.

**Confirm.**

```bash
docker exec filearr id                       # which uid:gid runs the process?
docker exec filearr ls -la <root_path>       # Permission denied?
ls -ln <host_path>                           # numeric owner/mode on the host
```

**Fix.** Unraid: run *Docker Safe New Permissions* on the share, or set
PUID/PGID in the template to the share owner. Compose / Proxmox: `chown` /
`chmod` the mount, or set `PUID`/`PGID` to the owning uid:gid. For SMB/NFS
mounts, permissions come from the mount options (`uid=`, `gid=`,
`file_mode=`, `dir_mode=`), not from `chown`.

**Data at risk.** None. A scan that cannot descend into a directory tombstones
that subtree; a later readable scan restores it.

### path-io {#path-io}

**Meaning.** Listing the directory raised an I/O error. When the message
matches a transport-class error (`Input/output error`, `Stale file handle`,
`Transport endpoint is not connected`, `Host is down`, timeouts) the title
says so: the mount is unhealthy.

**Confirm.** `dmesg | tail`, the rclone log, or the SMB/NFS server's own log.
`docker exec filearr ls <root_path>` reproduces the error.

**Fix.** Repair the underlying mount, then re-run a scan; tombstoned files
come back automatically.

**Data at risk.** None.

### path-empty {#path-empty}

**Meaning.** The directory exists, is readable, and lists **zero entries**.
This is almost always an *unmounted* share: what you are looking at is the
empty mount point. A scan over it **refuses to proceed** (see the
[empty-scan guard](#empty-scan-guard)) rather than tombstoning the library.

**Confirm.**

```bash
mount | grep <path>              # nothing listed = not mounted
docker exec filearr ls -la <root_path>
```

**Fix.** Mount the share (Unraid: check the Unassigned Devices / SMB mount is
up), then scan. Do not scan while it is empty if you can avoid it — the
recycle-bin retention protects tombstoned items meanwhile, but there is no
reason to spend it.

**Data at risk.** None within retention. See
[scan-tombstoned-all](#scan-tombstoned-all) if a scan already ran.

### path-ok {#path-ok}

**Meaning.** The path listed successfully. `ok` when the first listing was
fast; `warning` (**"but SLOW"**) when it took more than 2 s — a struggling
SMB/rclone mount. Scans will be slow and may time out.

**Fix (slow only).** Check the mount options (SMB: `cache=loose`, larger
`rsize`/`wsize`; rclone: `--vfs-cache-mode`, `--dir-cache-time`), server load,
and network. See [extraction throughput](../operations.md#extract-backpressure)
for how the worker adapts.

### watch-on-network {#watch-on-network}

**Meaning.** Watch mode is on and the root sits on a network filesystem.
inotify does not deliver events over SMB/NFS/FUSE reliably — changes may never
be seen.

**Fix.** Edit the library: turn watch mode off and set a scan schedule.

**Data at risk.** None; the catalogue is just stale until a scan runs.

### Last scan

### never-scanned {#never-scanned}

**Meaning.** No scan has ever run. Click **Scan** on the Libraries page.

### scan-failed {#scan-failed}

**Meaning.** The last scan crashed and the crash handler marked it `failed`.
The generic verdict; when the recorded error matches a known class the code
is one of the three below instead.

**Confirm.** The error text is on the verdict; the **Failed jobs** and
**Logs** sections carry the traceback if the worker recorded one; otherwise
`docker logs filearr-worker --since 1h`.

**Fix.** Fix the cause, then Scan again — a scan is idempotent. If it keeps
failing, see [scan-crash-loop](#scan-crash-loop). For a scan that never
finishes, see [scan runs stuck](../operations.md#scan-runs-stuck-in-stopping-or-orphaned-running).

**Data at risk.** None. A failed scan tombstones nothing.

### scan-failed-permission {#scan-failed-permission}

**Meaning.** The scan hit a permission error part-way through the tree —
the root is readable but a subdirectory is not.

**Confirm.** The error names the path; check it with
`docker exec filearr ls -la <that path>`.

**Fix.** Same as [path-permission](#path-permission), applied to the subtree.

### scan-failed-io {#scan-failed-io}

**Meaning.** The scan hit an I/O / transport error — the mount went away
mid-walk.

**Fix.** Same as [path-io](#path-io) / [path-hung](#path-hung); re-scan once
the mount is healthy.

### scan-failed-missing {#scan-failed-missing}

**Meaning.** The scan lost its root or a directory mid-walk ("No such file
or directory") — typically the share was unmounted while scanning.

**Fix.** Same as [path-missing](#path-missing); re-scan when mounted.

### scan-stopped {#scan-stopped}

**Meaning.** The last scan was stopped or cancelled — by a person, or by the
worker restarting mid-scan (the crash handler marks orphaned runs). Items seen
so far are kept; nothing was tombstoned.

**Fix.** Run the scan again to complete the walk.

### scan-running {#scan-running}

**Meaning.** A scan is running now. If it has been running far longer than
usual with a rate of 0/s, the worker is probably stuck on a hung mount.

**Fix.** Watch the live rate on the Libraries page; use **Stop**, fix the mount,
then re-scan.

### scan-tombstoned-all {#scan-tombstoned-all}

**Meaning.** The last scan saw **zero** files and tombstoned every item as
`missing`. Almost always an unmounted share at scan time — the tree looked
empty. With the [empty-scan guard](#empty-scan-guard) on (the default) this
verdict only appears for scans that were **forced** (`force_empty`) or run
with the guard disabled.

**Confirm.** The path verdict above it usually says `path-empty` or
`path-missing`.

**Fix.** Fix the mount, then Scan. **Nothing was deleted** — the next
successful scan restores every item, including its user metadata edits.

**Data at risk.** Only if the situation persists past the recycle-bin
retention. Consider raising `FILEARR_RECYCLE_RETENTION_DAYS` while you fix a
long outage.

### The empty-scan guard {#empty-scan-guard}

A full scan whose walk sees **zero entries** over a library that previously
held active items is refused: the run is marked `failed` with the message
*"walk saw an empty tree but the library holds N active items … if the
library really was emptied, rescan with force_empty"*, and **nothing is
tombstoned**. This closes the classic dead-FUSE/SMB hole — a bind that
presents as a readable-but-empty mountpoint — which used to tombstone a whole
library in one pass and then age it out on the recycle-bin schedule.

When the library genuinely was emptied and you want the catalog to follow:

```bash
curl -X POST "http://filearr.example.com:8484/api/v1/libraries/<id>/scan?force_empty=true" \
  -H "Authorization: Bearer $FILEARR_ADMIN_KEY"
```

(one run only — the next scheduled scan is guarded again). To switch the guard
off permanently set `FILEARR_SCAN_EMPTY_GUARD=false`. A missing or unreadable
root is a separate, earlier check (`path-missing`) and always fails the scan.

### scan-many-missing {#scan-many-missing}

**Meaning.** The last scan tombstoned more items than it saw. A subtree went
away — a renamed folder, an unmounted sub-share, a moved collection.

**Fix.** A rename is best handled by moving the files back or, when the
whole library moved, editing the library root. If the subtree is gone for
good, the tombstones expire with retention.

### scan-ok {#scan-ok}

**Meaning.** The last scan finished normally; the numbers are in the detail.

### scan-crash-loop {#scan-crash-loop}

**Meaning.** Two or more consecutive scans failed. The schedule will keep
hitting the same failure. Do not just re-run — read the error in the evidence
block and in the Failed jobs / Logs sections.

**Fix.** Address the underlying verdict (usually a path-* one). If every scan
fails on the same *file* rather than the mount, open an issue with the copied
report.

### Extraction errors

Extraction failures are per item. The item is still indexed by name and path;
only the deep metadata (duration, codec, EXIF, text …) is missing. The
**Errors** column on the Libraries page opens the per-item list with the kind and
message for each; **Retry extracts** re-queues them.

### extract-dependency {#extract-dependency}

**Meaning.** Items failed because a **tool is missing** from the container
(ffprobe, exiftool, pdftotext, tesseract …). This is a deployment problem, not
a file problem: every affected file fails the same way until the tool exists.

**Confirm.** The About page lists detected tools; `docker exec filearr which
ffprobe exiftool` from a shell.

**Fix.** Use the image that bundles the tool (the official image does) or
install it; then **Retry extracts** on the library.

**Data at risk.** None.

### extract-corrupt {#extract-corrupt}

**Meaning.** The parser rejected the file's own bytes — truncated download,
bad tags, encrypted PDF. Expected on real libraries.

**Fix.** Open the failing-items list to see which files; replace or ignore.
Retry only if you fixed the files.

### extract-guard {#extract-guard}

**Meaning.** A deliberate safety ceiling (file too large, too many pages,
decode budget) stopped extraction. Not an error to fix unless you want those
files processed.

**Fix.** Raise the relevant `FILEARR_*` extraction limit
([configuration reference](../reference/configuration.md)) and Retry extracts.

### extract-error {#extract-error}

**Meaning.** Items failed with an I/O or unexpected error — usually a read
that broke mid-way during a network-mount hiccup. Also shown for errors
recorded before error classification existed.

**Fix.** Confirm the mount is healthy, then **Retry extracts**. If the same
messages recur, the *top messages* list in the Extraction errors section is
what to search the Logs panel for.

### extract-ok {#extract-ok}

**Meaning.** Every active item extracted cleanly.

### Jobs and schedule

### jobs-failed {#jobs-failed}

**Meaning.** Background jobs referencing this library exhausted their retries.
Their recorded error (if the worker captured one) is in the Failed jobs
section.

**Fix.** Read the error; scan and extract jobs are safe to re-trigger via
**Scan** / **Retry extracts**. Clear the history from the Jobs page once
understood. For stalled or storming jobs see
[the reaper](../operations.md#scan-scheduling-storms-stalled-jobs-the-reaper).

### disabled {#disabled}

**Meaning.** The library is disabled: scheduled scans skip it, manual scans
still work. Enable it in Edit when you want the schedule back.

## Reading the raw sections {#raw-sections}

- **Path** — the container-side probe: `exists` / `is_dir` / `readable`,
  whether the filesystem is a network one and its type, how long the first
  listing took, how many entries were seen (capped at 2000) and a sample of
  names. `skipped: agent-owned` for agent libraries.
- **Recent scans** — the last six runs with status, start, duration and the
  `seen` / `new` / `changed` / `missing` / `excluded` counters plus the
  recorded error. A run whose `seen` is 0 and `missing` is large is the
  unmounted-share signature.
- **Extraction errors** — total count, a per-kind breakdown (`dependency`,
  `corrupt`, `guard`, `error`) and the most frequent messages. The per-item
  list lives behind the Errors column on the Libraries page.
- **Failed jobs** — up to ten failed jobs whose arguments name this library
  (task, queue, attempts, scheduled time, last error).
- **Agent** — only for agent-owned libraries: name, host, platform, last
  contact, online / revoked.
- **Logs** — the newest 25 warning-or-higher lines from the unified log that
  mention the library id or name (see [the Logs panel](../operations.md#logs-panel)).
- **Context** — recycle-bin retention and worker concurrency in effect.

## Still stuck? {#still-stuck}

Click **Copy report** in the dialog and paste the JSON into a new issue at
<https://github.com/pwsh/filearr/issues>, together with the deployment type
(Unraid / Compose / Proxmox), how the media path is mounted (local, SMB,
NFS, rclone) and what you have already tried. Paths and error messages in the
report are sanitised of secrets, but do skim it before posting.
