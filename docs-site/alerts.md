# Alerts & notifications

Filearr can tell you when something changes in a library (a file appeared,
changed, vanished or moved) and when something is wrong with Filearr itself
(a scan failed, a disk is filling up, an agent went dark, permissions
drifted). Both kinds flow through the same three pieces on the **Alerts**
tab:

- **Channels** — *where* a notification goes: a webhook (generic JSON,
  Discord or Slack), an SMTP mailbox, or an [Apprise](https://github.com/caronc/apprise)
  URL (ntfy, Gotify, Telegram, Pushover, Matrix, … — 100+ services).
- **Rules** — *what* fires and how it is batched. File-change rules match a
  library, a path glob and event types; **system rules** ship with the
  product for operational faults.
- **Events** — the delivery log: every batch that fired, its status
  (pending / delivered / failed) and, when it failed or was suppressed, why.

Everything here needs the **admin** scope. Channel secrets (webhook HMAC
secret, SMTP password, the whole Apprise URL) are encrypted at rest with
`FILEARR_SECRET_KEY` — see [Operations → backup and restore](operations.md#backup-and-restore)
for why that key must travel with your backups.

## Quick start (five minutes)

1. **Alerts → Channels → New channel.** Pick a type and paste the target
   (for Discord/Slack the payload format auto-detects from the URL). Save,
   then press **Test** on the row — a test message must arrive before you
   go further. The test result shows the exact refusal reason if not.
2. **Alerts → Rules.** Open a **System:** rule (start with *System: scan
   failure* and *System: low disk space*), tick your channel, **enable** it.
3. **New rule** for a library you care about: name it, choose the library
   (or *all*), optionally a path glob (`Movies/**`, `*.mkv`), tick the event
   types, pick *immediate* or a *digest*, attach the channel. Save.
4. Make a change in that library and rescan (or wait for the schedule) —
   the **Events** tab shows the batch and your channel receives it.

!!! warning "System rules ship disabled and unattached"
    Every built-in rule is seeded **disabled with no channel** so a fresh
    install never pages anyone by surprise. Nothing operational is delivered
    until you attach a channel and enable the rule. Do this on day one for
    *scan failure* and *low disk space* at least.

## Channels

| Type | Config | Notes |
| --- | --- | --- |
| `webhook` | `url`, optional `secret`, `webhook_format` (`generic` / `discord` / `slack`) | **generic** posts Filearr's own JSON and signs it with `X-Filearr-Signature: t=<unix>,sha256=<hex>` (HMAC-SHA256 of `"<t>." + body` with your secret; verify in constant time and reject timestamps older than `FILEARR_ALERT_SIGNATURE_MAX_AGE_S`, 300 s). **discord** / **slack** reshape the body to what those services accept (embeds / blocks; limits and escaping handled) and carry no HMAC. Format auto-detects from `discord.com/api/webhooks/…` and `hooks.slack.com/…`. |
| `email` | `host`, `port` (587), `security` (`starttls` / `ssl` / `plain`), `username`, `password`, `from_addr`, `to` (list) | Plain SMTP is refused unless `allow_insecure: true`. One message per batch; digests arrive as one message per window. |
| `apprise` | `url` | The whole URL is the secret (tokens are inline). `apprise` is installed in the image; the channel **Test** proves the scheme is supported. Timeout `FILEARR_ALERT_APPRISE_TIMEOUT_S` (30 s). |

**Outbound safety (all webhook formats).** The target host is resolved and
**every** A/AAAA answer is vetted before the request; private, loopback,
link-local (cloud metadata) and reserved addresses are refused by default, the
socket is pinned to the vetted IP (DNS-rebinding defence), redirects are never
followed, and the response is size- and time-capped. To reach a **LAN** target
either flip the blunt switch `FILEARR_WEBHOOK_ALLOW_PRIVATE_CIDRS=true`
(RFC1918/ULA only; loopback and link-local stay denied) or, better, list the
exact targets in `FILEARR_WEBHOOK_ALLOWED_CIDRS=127.0.0.1/32,10.0.0.5/32` — a
host whose DNS mixes an allowed and a blocked answer is still refused.

**Locality.** A channel is `central` (the server delivers) or `agent` (reserved
for agent-local delivery; central never uses it). Leave `central`.

## Rules

### File-change rules

| Field | Meaning |
| --- | --- |
| Library | one library or *all* |
| Path glob | gitignore-style pattern over the item's relative path (`Movies/**`, `**/*.nfo`); empty = everything |
| Event types | `created`, `modified`, `deleted`, `moved` (one or more) |
| Hash-change only | for `modified`: fire only when the content hash actually changed (size/mtime churn alone stays quiet). Needs a hash policy that computes content hashes. |
| Throttle | **immediate** — batch matches for *group wait* seconds (default 30) then send one notification per group; **digest** — collect into an *hourly* or *daily* window and send one summary per window |
| Repeat interval | re-notify a still-firing group every N seconds (blank = never repeat; new matches after `FILEARR_ALERT_GROUP_INTERVAL_S` (300 s) always re-notify) |
| Group by *(advanced)* | always per event type + library + rule; add **top-level folder**, **file extension** or **each file** to split one library-wide batch into finer notifications |
| Inhibited by *(advanced)* | mute this rule while any selected rule has fired in the last *N* seconds (default 900) for the same library (or a library-wide inhibitor). Muted batches are recorded as `suppressed: inhibited by …` and never sent — e.g. let *System: agent offline* silence *System: agent replication stalled*, or a coarse "anything changed" rule silence a noisy per-file one. |

Events are evaluated **at scan time** (the scan classifies each transition,
so a rule sees the same truth the catalog records) and on agent replication
for agent libraries. Matches are written to the events table immediately;
delivery is the job of the **dispatch pump** (every minute), which groups,
waits, digests, retries and enforces the per-rule hourly ceiling
(`FILEARR_ALERT_RULE_MAX_PER_HOUR`, 100 batches) — a runaway glob is held
with a `suppressed: rule hit hourly dispatch ceiling` note rather than
flooding your phone.

### System rules

| Rule | Fires when | Knobs |
| --- | --- | --- |
| System: scan failure | a scan run ends `failed` | — |
| System: extract-error spike | a library's extract errors grow by more than the threshold inside the window | `FILEARR_ALERT_ERROR_SPIKE_THRESHOLD` (50) / `_WINDOW_S` (3600); both editable on the rule |
| System: low disk space | a watched volume crosses warn/critical (and again on recovery) | `FILEARR_DISK_*` — see [Operations → disk fills up](operations.md#disk-fills-up-unbounded-generation-postgres-crash) |
| System: scheduled report delivery failure | a scheduled report export cannot be delivered after its retry budget | — |
| System: agent offline | a cert-bound, non-revoked agent has not been seen for `FILEARR_AGENT_OFFLINE_ALERT_SECONDS` (48 h — offline is a *normal* laptop state, so this is deliberately soft); recovery event when it returns | thresholds are env |
| System: agent replication stalled | an agent is **online but silent** (replication watermark older than `FILEARR_AGENT_REPLICATION_STALL_ALERT_SECONDS`, 6 h); recovery event when it resumes | — |
| System: agent verification mismatch | an agent `stat_check`/`rehash_check` reports a hosted item gone or changed versus the catalog | — |
| System: permission change | a re-inventoried path's permissions differ from its previous snapshot (ACEs added/removed/modified, owner/group changed) — see [Reports → Permission drift](reports.md#permission-drift) | — |

Only the channels, enabled flag, throttle/timings and inhibition are editable
on a system rule; its match logic is fixed. System events dedupe per hour and
per subject (a failing scan re-fires at most hourly; a disk that flaps does
not spam).

### Mass edit

Select rules with the checkboxes and a bulk bar appears: switch the throttle
(immediate with a group wait, or an hourly/daily digest) and/or change the
channel attachment (**replace with** / **also publish to** / **stop publishing
to**) for every selected rule in one call — the "move everything off the old
webhook and onto ntfy" chore. System rules accept the same bulk throttle and
channel changes as single edits.

## Events tab

Newest first, filterable by status. Each row is one **batch** (group) with the
rule, event type, count, the first paths, delivery status and `last_error`:

- **pending** — written, not yet due (group wait / digest window) or no
  enabled central channel is attached yet.
- **delivered** — sent (or consumed: `suppressed: inhibited by …` means it was
  deliberately muted).
- **failed** — every retry (`FILEARR_ALERT_MAX_DELIVERY_ATTEMPTS`, 5) was
  refused; `last_error` carries the reason (an SSRF refusal is permanent and
  not retried; a 5xx/timeout is).

Terminal events age out after `FILEARR_ALERT_EVENTS_RETENTION_DAYS` (30);
pending ones are never purged. The summary strip at the top is what to glance
at after changing a channel: a climbing *failed* count with the same
`last_error` means the channel is broken, not the rules.

## Common recipes

- **"Tell me when a new movie lands."** Rule on the Movies library, `created`,
  glob `**/*.{mkv,mp4}`, immediate with a 120 s group wait (a rip drops several
  files at once — you want one message, not six). Group by *top-level folder*
  if several people drop into different subfolders.
- **"Nightly summary of everything that changed."** One rule, all libraries,
  all event types, *daily* digest, e-mail channel.
- **"Page me if the NAS fills up or a scan breaks, but never about laptops
  sleeping."** Enable *low disk space* + *scan failure* on a push channel;
  leave *agent offline* at its 48 h default or disabled; enable *agent
  replication stalled* (the sharper signal).
- **"Did anyone widen permissions on the finance share?"** Schedule the
  `permissions` inventory collector on that agent, enable *System: permission
  change*, and keep the `permission_changes` report bookmarked for the
  before/after detail.
- **"Discord keeps rejecting my webhook."** The channel is on the `generic`
  format; switch it to `discord` (or recreate it from the Discord URL so it
  auto-detects) — see [Operations → alerting doesn't fire](operations.md#alerting-doesnt-fire).

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Nothing ever arrives | Is the rule **enabled** and does it have a channel? Is the channel enabled? Did the channel **Test** succeed? Look at the Events tab — `pending` forever with no channel attached is the classic. |
| `webhook target refused (blocked:private)` | LAN target: set `FILEARR_WEBHOOK_ALLOWED_CIDRS` (exact) or `FILEARR_WEBHOOK_ALLOW_PRIVATE_CIDRS=true`. |
| `dispatch held (secret)` | `FILEARR_SECRET_KEY` is missing or changed since the channel was saved — restore the original key (see backup notes) or re-enter the secret. |
| `suppressed: rule hit hourly dispatch ceiling` | The rule is too broad or the glob matches a bulk import; switch it to a digest, narrow the glob, or raise `FILEARR_ALERT_RULE_MAX_PER_HOUR`. |
| Emails go to spam / are refused | Use an authenticated submission port (587 STARTTLS / 465 SSL) and a `from_addr` your provider accepts. |
| Apprise: `unsupported URL` | The scheme is not one apprise knows, or the extra it needs is not in the image; the channel Test names it. |

## API

`GET/POST /api/v1/alert-channels`, `PATCH/DELETE /api/v1/alert-channels/{id}`,
`POST /api/v1/alert-channels/{id}/test`, `GET/POST /api/v1/alert-rules`,
`PATCH/DELETE /api/v1/alert-rules/{id}`, `GET /api/v1/alert-events`,
`GET /api/v1/alert-events/summary` — all admin scope; full schemas at
`/api/docs`. Secrets are write-only: reads return `__redacted__`, and sending
`__unchanged__` on an edit keeps the stored ciphertext.
