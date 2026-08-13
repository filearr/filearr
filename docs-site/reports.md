# Reports & exports

Filearr answers questions about your files — *what is duplicated, what has not
been touched in years, what is enormous, what failed to probe* — and hands you
the answer as a file you can act on.

!!! quote "What Filearr will and will not do"
    **Filearr never modifies, moves or deletes your media.** Media mounts are
    read-only and every report is a read-only query. When a report tells you that
    four copies of the same 14 GB film exist, Filearr's job ends at *telling you*.
    Reclaiming the space is your script, on your machine, under your control —
    and this page hands you those scripts.

    That is a deliberate design decision, not a missing feature. A catalog that
    also deletes is a catalog you cannot trust when it is wrong.

!!! note "Placeholders"
    Examples use `filearr.example.com` and `/mnt/user/media`; substitute your own
    host and paths. The API is assumed at `:8484`.

## Canned reports {#canned-reports}

Canned reports are built in — no query to write, no definition to save. List them
with their metadata (columns, defaults, whether they take a library or a
threshold):

```bash
curl -s http://filearr.example.com:8484/api/v1/reports | jq '.reports[].id'
```

| Report | What it answers |
|---|---|
| `unmapped_extensions` | Which extensions landed in the "other" catch-all — feeds extension-map expansion. |
| `bad_mtime` | Files dated more than 48 h in the **future** (bad clock, timezone bug, corrupt timestamp). |
| `corrupt_media` | Items that recorded an extraction error, split into ffprobe/decode rejections vs. tag-parser errors. |
| `largest_files` | Top N by size. |
| `largest_folders` | Every folder at every depth with its **recursive** (du-style) total. |
| `low_quality_video` | Probed video scored against resolution / codec / bitrate-per-pixel floors. |
| `duplicate_files` | One row **per duplicate group**: copy count, hash tier, wasted bytes. The overview. |
| `duplicate_files_detail` | One row **per copy**. The actionable one — see [Acting on the duplicate copies export](#acting-on-duplicates). |
| `stale_files` | Files not **modified** in *N* days (default 730) — see [Staleness](#staleness). |

Run one as a paginated JSON page (this is what the Reports screen shows):

```bash
curl -s "http://filearr.example.com:8484/api/v1/reports/duplicate_files_detail?limit=50" \
  -H "Authorization: Bearer $FILEARR_API_KEY" | jq '.rows[0]'
```

Common parameters:

`library_id`
:   Restrict to one library. Rejected with 422 by reports that do not support it.

`limit` / `offset`
:   JSON paging. For a **capped** report (`largest_files`, `largest_folders`)
    `limit` is the report's definitional top-N and bounds the export too.

`threshold_days`
:   Only for reports whose metadata sets `supports_threshold` (today:
    `stale_files`). Validated **1–36500**; rejected with 422 on any other report,
    so a typo never silently does nothing.

`format`
:   `json` (default, the paginated envelope) or an export format — next section.

### Path columns {#path-columns}

Every per-item report row carries four path forms, because the path Filearr sees
inside its container is rarely the path *your* machine uses:

| Column | What it is |
|---|---|
| `path` | Container-absolute, as the scanner saw it (`/data/media/movies/x.mkv`). |
| `native_path` | The library's `native_prefix` joined to the item — the path on the **source system** (`/mnt/user/media/movies/x.mkv`). Empty when the library maps no native prefix. |
| `share_url` | The network location (`smb://tower/media/...`), from `share_prefix` or the deploy mount map. |
| `share_unc` | The Windows UNC form of the same (`\\tower\media\...`). Empty for non-SMB schemes. |

Scripts should resolve a path in the order **`native_path` → `share_unc` → `path`**,
and the script must run somewhere that path is actually visible. `path` is the
last resort precisely because it is only meaningful *inside the Filearr
container* — if you fall back to it, run the script there, or on a host whose
mounts match.

JSON/NDJSON/XML rows additionally carry `item_id` (so a row can be opened in the
UI, or PATCHed through the API). CSV and XLSX deliberately omit it — an id is
noise in a spreadsheet.

## Exports {#exports}

Four machine-readable formats, all streamed off a server-side cursor so a
750 000-row export costs about one row of memory:

| Format | Use it for |
|---|---|
| `ndjson` | **Scripting.** One JSON object per line; nothing is escaped or mangled; `item_id` and every column present. The recommended input for the scripts below. |
| `csv` | Spreadsheets and PowerShell's `Import-Csv`. Declared columns only, and see the [formula guard](#csv-formula-guard). |
| `xml` | Legacy ingestion. Fully escaped, well-formed. |
| `xlsx` | Handing a result to a human. |

### Synchronous export

Add `format=` to the run endpoint. You get the file directly, with a
`Content-Disposition` filename:

```bash
curl -s -H "Authorization: Bearer $FILEARR_API_KEY" \
  -o duplicates.ndjson \
  "http://filearr.example.com:8484/api/v1/reports/duplicate_files_detail?format=ndjson"
```

For a report that is *not* capped, omitting `limit` streams the whole result;
passing `limit` caps the rows.

### Background export

For a very large result — or to have it delivered on a schedule — queue the
export instead. It runs on the worker, writes to a staging file, and is fetched
afterwards:

```bash
# 1. queue it (202 Accepted, returns the export row)
ID=$(curl -s -X POST -H "Authorization: Bearer $FILEARR_API_KEY" \
  "http://filearr.example.com:8484/api/v1/reports/stale_files/export?format=ndjson&threshold_days=1095" \
  | jq -r .id)

# 2. poll until status == complete
curl -s -H "Authorization: Bearer $FILEARR_API_KEY" \
  "http://filearr.example.com:8484/api/v1/exports/$ID" | jq '{status, row_count, expires_at}'

# 3. download
curl -s -H "Authorization: Bearer $FILEARR_API_KEY" \
  -o stale.ndjson "http://filearr.example.com:8484/api/v1/exports/$ID/download"
```

Every parameter the sync endpoint accepts — `library_id`, `limit`,
`threshold_days` — is stored on the export row and rebuilt by the job, so a
queued export runs exactly the query you asked for.

### Retention and access {#export-retention}

- **Artifacts expire.** Each completed export gets an `expires_at`
  (`FILEARR_EXPORT_TTL_HOURS`). A scheduled purge deletes the file and stamps
  `purged_at`, **keeping the row** as an audit trail. Fetching a purged export
  returns `410`.
- **Exports need the `download` permission**, not merely read. Viewing a report on
  screen and pulling the whole thing out as a file are different acts; a scoped
  principal can be granted one without the other. Row scoping is applied *before*
  grouping, so a file you may not see never appears — and never contributes to a
  duplicate group's copy count either.
- **Every served download is audited unconditionally**, regardless of the
  read-audit setting. An export is data-exfiltration shaped, even when it is
  "only metadata".
- **Concurrency is capped per principal** (`FILEARR_EXPORT_MAX_ACTIVE`); over the
  cap, an enqueue returns `429`.

### The CSV formula guard {#csv-formula-guard}

Catalog data is untrusted input — a filename is whatever someone typed. Any CSV
or XLSX cell whose first character is `=`, `+`, `-`, `@`, a tab or a carriage
return is written with a leading single quote so a spreadsheet cannot execute it
(OWASP CSV injection).

That is the right default, but it means **a path beginning with `-` or `=` comes
back with a `'` in front of it**. If you parse the CSV, strip that quote when the
character after it is one of the guarded set — the PowerShell script below does
exactly this. NDJSON is not affected (JSON needs no such guard), which is the
main reason the shell examples use NDJSON.

## Staleness {#staleness}

`stale_files` lists files whose **last-modified** time is older than
`threshold_days` (default 730), oldest first, with `age_days` alongside.

!!! warning "This is modification age, not access age"
    Filearr does not capture filesystem **access** times at all, and it will not
    guess at them: `atime` is disabled outright on most real-world mounts
    (`noatime` is the norm, and network mounts are worse), so an access-based
    report would be confidently wrong. "Stale" here means **unmodified**, not
    "unread". A film you rewatch every year and never edit is stale by this
    definition. Treat the list as *candidates for a human look*, not as
    "nobody wants these".

```bash
# everything untouched for three years, in one library, as a spreadsheet
curl -s -H "Authorization: Bearer $FILEARR_API_KEY" -o stale.xlsx \
  "http://filearr.example.com:8484/api/v1/reports/stale_files?format=xlsx&threshold_days=1095&library_id=$LIB"
```

## Acting on the duplicate copies export {#acting-on-duplicates}

`duplicate_files_detail` emits **one row per copy**, which is what makes it
scriptable. The columns that matter:

| Column | Meaning |
|---|---|
| `group_key` | Identifies the duplicate group (the content hash, or `quickhash:size` for a fallback group). Rows of a group are contiguous in the export. |
| `group_rank` | `0` = newest copy by mtime; ties broken by item id so **re-runs are stable**. |
| `keep_hint` | `keep` for rank 0, `candidate` for the rest. |
| `copies_in_group` | How many copies exist. |
| `hash_tier` | `content_hash` (byte-verified) or `quick_hash` ([sampled](#hash-tier-caveat)). |
| `size`, `mtime` | The values Filearr recorded at scan time — the scripts **re-verify both** against the live file. |

Groups are ordered biggest-waste-first, so even a truncated export is the rows
you most want, whole groups first.

!!! info "`keep_hint` is data, not a decision"
    "Newest mtime wins" is one reasonable default and nothing more. It is *not* a
    recommendation from Filearr about which copy matters — the newest copy may be
    the re-encode you regret, and the oldest may be the original. `keep_hint`
    exists so a script has a deterministic starting point; read the groups before
    you trust it, and re-sort on your own rule if you have one (path prefix,
    library, size) — every row carries what you need to.

### Before you run anything {#hash-tier-caveat}

!!! danger "A `quick_hash` group is a sampled signal, not proof"
    Filearr hashes small files (≤128 KiB) in full, but larger files get a
    **sampled** `quick_hash` — head and tail windows, not the whole file. Two
    files with the same `quick_hash` and the same size are *very likely*
    identical and are **not guaranteed** to be. Rows in a `quick_hash`-tier group
    are candidates for verification, not for deletion.

    Every script below therefore **skips `quick_hash` groups by default**. Opt in
    with `--allow-quick-hash` only if you have verified them another way, or use
    the `--verify-hash` option, which re-hashes the two files and compares them
    directly.

The hash verification option needs **`xxhsum`** (from `xxhash`) — Filearr's
hashes are **xxh3 / xxh128**, not SHA-256, so `sha256sum` will not reproduce them
and comparing against it is meaningless. `xxhsum` is a one-package install on
Linux (`apt install xxhash`, `dnf install xxhash`) and macOS
(`brew install xxhash`); on Windows it ships with the
[xxHash releases](https://github.com/Cyan4973/xxHash/releases). It is entirely
optional: the size + mtime verification every script does by default uses only
tools already on the machine.

### What all three scripts do

1. **Dry run by default.** They print what they *would* do. Nothing happens
   without an explicit `--execute` / `-Execute`.
2. **Never touch a `keep_hint == "keep"` row.** Ever.
3. **Re-verify before acting, per file.** The live file's size (and mtime, within
   two seconds) must match the export row. A stale export — a rescan, an edit, a
   move since the export ran — makes that file **fail closed and get skipped**,
   not deleted. This is the single most important property: an export is a
   snapshot, and acting on a snapshot without re-checking is how people lose
   data.
4. **Skip `quick_hash` groups** unless told otherwise.
5. **Report a summary** — acted, skipped-missing, skipped-mismatch, bytes
   reclaimed.

Three actions are offered in each: `delete`, `quarantine` (move to a holding
area, mirroring the original path — reversible, and the right default for a
first run), and `hardlink` (replace the copy with a hard link to the keeper, so
both paths keep working and the bytes are stored once).

=== "Linux (bash + jq)"

    **Dependencies: `jq` only.** That is a deliberate choice, not laziness — the
    export is CSV *or* NDJSON, and CSV containing quoted commas and embedded
    newlines cannot be parsed safely in pure `awk`/`cut`. Any "clever" awk CSV
    parser is a data-loss bug waiting for the first filename with a comma in it.
    NDJSON plus `jq` is correct for every filename. Paths are passed between `jq`
    and the shell **base64-encoded** for the same reason: a filename may contain
    spaces, quotes, tabs or newlines.

    ```bash title="filearr-dedupe.sh"
    #!/usr/bin/env bash
    # Act on a Filearr duplicate_files_detail NDJSON export.
    # DRY RUN unless --execute is passed. Never touches keep_hint=="keep".
    set -euo pipefail

    EXPORT=""; MODE="quarantine"; QDIR=""; EXECUTE=0
    ALLOW_QUICK=0; VERIFY_HASH=0; MTIME_TOLERANCE=2

    usage() {
      cat <<'USAGE'
    Usage: filearr-dedupe.sh --export FILE [options]
      --export FILE        NDJSON export of duplicate_files_detail (required)
      --mode MODE          quarantine (default) | delete | hardlink
      --quarantine DIR     holding area for --mode quarantine (required for it)
      --execute            actually do it (default is a dry run)
      --allow-quick-hash   also act on sampled quick_hash groups (see the docs)
      --verify-hash        additionally require xxhsum agreement with the keeper
    USAGE
    }

    while [ $# -gt 0 ]; do
      case "$1" in
        --export)           EXPORT="$2"; shift 2 ;;
        --mode)             MODE="$2"; shift 2 ;;
        --quarantine)       QDIR="$2"; shift 2 ;;
        --execute)          EXECUTE=1; shift ;;
        --allow-quick-hash) ALLOW_QUICK=1; shift ;;
        --verify-hash)      VERIFY_HASH=1; shift ;;
        -h|--help)          usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
      esac
    done

    [ -n "$EXPORT" ] && [ -r "$EXPORT" ] || { echo "--export FILE is required" >&2; exit 2; }
    command -v jq >/dev/null || { echo "jq is required (apt/dnf install jq)" >&2; exit 2; }
    case "$MODE" in
      quarantine) [ -n "$QDIR" ] || { echo "--quarantine DIR is required" >&2; exit 2; } ;;
      delete|hardlink) ;;
      *) echo "unknown --mode $MODE" >&2; exit 2 ;;
    esac
    if [ "$VERIFY_HASH" -eq 1 ]; then
      command -v xxhsum >/dev/null || { echo "--verify-hash needs xxhsum" >&2; exit 2; }
    fi

    # ---- shared jq preamble -------------------------------------------------
    # to_epoch: the export's mtime is ISO-8601 with a numeric offset
    # ("2026-08-13T05:34:28.132501+00:00"). Drop the fraction, normalise a "Z",
    # then subtract the offset so it compares against stat's UTC epoch seconds.
    # best_path: the documented fallback order native_path -> share_unc -> path.
    JQ_PRELUDE='
      def to_epoch:
        sub("\\.[0-9]+"; "") | sub("Z$"; "+00:00")
        | capture("^(?<b>.{19})(?<sign>[+-])(?<hh>[0-9]{2}):?(?<mm>[0-9]{2})$") as $c
        | (($c.b + "Z") | fromdateiso8601)
          - ((if $c.sign == "-" then -1 else 1 end)
             * (($c.hh | tonumber) * 3600 + ($c.mm | tonumber) * 60));
      def best_path:
        (.native_path // "") as $n | (.share_unc // "") as $u
        | if ($n | length) > 0 then $n
          elif ($u | length) > 0 then $u
          else .path end;
    '

    # ---- pass 1: remember each group's keeper (hardlink / hash verify need it)
    declare -A KEEP_B64 KEEP_SIZE
    while IFS=$'\t' read -r gk pb64 sz; do
      KEEP_B64["$gk"]="$pb64"; KEEP_SIZE["$gk"]="$sz"
    done < <(jq -r "$JQ_PRELUDE"'
      select(.keep_hint == "keep")
      | [ .group_key, (best_path | @base64), (.size | tostring) ] | @tsv
    ' "$EXPORT")

    # ---- pass 2: the candidates --------------------------------------------
    acted=0; missing=0; mismatch=0; skipped_tier=0; reclaimed=0
    while IFS=$'\t' read -r gk pb64 sz mt tier copies; do
      path="$(printf '%s' "$pb64" | base64 -d)"

      if [ "$tier" != "content_hash" ] && [ "$ALLOW_QUICK" -eq 0 ]; then
        skipped_tier=$((skipped_tier + 1)); continue
      fi
      if [ ! -f "$path" ]; then
        echo "SKIP  missing:       $path" >&2; missing=$((missing + 1)); continue
      fi

      # Fail closed on ANY disagreement with the export snapshot.
      live_size=$(stat -c%s -- "$path")
      live_mtime=$(stat -c%Y -- "$path")
      if [ "$live_size" != "$sz" ]; then
        echo "SKIP  size changed:  $path ($live_size != $sz)" >&2
        mismatch=$((mismatch + 1)); continue
      fi
      delta=$(( live_mtime - mt )); [ "$delta" -lt 0 ] && delta=$(( -delta ))
      if [ "$delta" -gt "$MTIME_TOLERANCE" ]; then
        echo "SKIP  mtime changed: $path" >&2
        mismatch=$((mismatch + 1)); continue
      fi

      keeper=""
      if [ -n "${KEEP_B64[$gk]:-}" ]; then
        keeper="$(printf '%s' "${KEEP_B64[$gk]}" | base64 -d)"
      fi
      if [ "$MODE" = "hardlink" ] || [ "$VERIFY_HASH" -eq 1 ]; then
        if [ -z "$keeper" ] || [ ! -f "$keeper" ]; then
          echo "SKIP  keeper missing (group $gk): $path" >&2
          mismatch=$((mismatch + 1)); continue
        fi
        if [ "$(stat -c%s -- "$keeper")" != "${KEEP_SIZE[$gk]}" ]; then
          echo "SKIP  keeper changed (group $gk): $path" >&2
          mismatch=$((mismatch + 1)); continue
        fi
      fi
      if [ "$VERIFY_HASH" -eq 1 ]; then
        # Compare the two files to EACH OTHER (xxh128). This never depends on how
        # Filearr stored its digest -- it only asks "are these the same bytes?".
        a=$(xxhsum -H2 -- "$keeper" | awk '{print $1}')
        b=$(xxhsum -H2 -- "$path"   | awk '{print $1}')
        if [ "$a" != "$b" ]; then
          echo "SKIP  hash differs:  $path" >&2
          mismatch=$((mismatch + 1)); continue
        fi
      fi

      case "$MODE" in
        delete)     action="delete           $path (1 of $copies)" ;;
        quarantine) dest="$QDIR/${path#/}"; action="quarantine       $path -> $dest" ;;
        hardlink)   action="hardlink to keep $path -> $keeper" ;;
      esac

      if [ "$EXECUTE" -eq 0 ]; then
        echo "DRY   would $action"
      else
        case "$MODE" in
          delete)     rm -f -- "$path" ;;
          quarantine) mkdir -p -- "$(dirname -- "$dest")"; mv -- "$path" "$dest" ;;
          hardlink)
            # ln -f REPLACES the candidate with a hard link to the keeper: both
            # paths keep working and the bytes are stored once. Hard links cannot
            # cross filesystems -- if the copies live on different mounts this
            # fails, and failing is correct (see the symlink note below).
            ln -f -- "$keeper" "$path" ;;
        esac
        echo "DONE  $action"
      fi
      acted=$((acted + 1)); reclaimed=$((reclaimed + sz))
    done < <(jq -r "$JQ_PRELUDE"'
      select(.keep_hint == "candidate")
      | [ .group_key, (best_path | @base64), (.size | tostring),
          (.mtime | to_epoch | tostring), .hash_tier, (.copies_in_group | tostring) ]
      | @tsv
    ' "$EXPORT")

    if [ "$EXECUTE" -eq 1 ]; then verb="Acted on"; else verb="Would act on"; fi
    printf '\n%s: %d file(s), %d bytes (%s)\n' "$verb" "$acted" "$reclaimed" "$MODE"
    printf 'skipped: %d missing, %d changed since export, %d sampled-hash group(s)\n' \
      "$missing" "$mismatch" "$skipped_tier"
    [ "$EXECUTE" -eq 1 ] || printf 'This was a DRY RUN. Re-run with --execute.\n'
    ```

    Typical first use — look, then quarantine, and only much later empty the
    holding area:

    ```bash
    curl -s -H "Authorization: Bearer $FILEARR_API_KEY" -o dups.ndjson \
      "http://filearr.example.com:8484/api/v1/reports/duplicate_files_detail?format=ndjson"

    ./filearr-dedupe.sh --export dups.ndjson --quarantine /mnt/user/quarantine
    ./filearr-dedupe.sh --export dups.ndjson --quarantine /mnt/user/quarantine --execute
    ```

    !!! tip "Hard link or symlink?"
        `ln -f` (hard link) is what the script does: the two paths become the same
        inode, so the bytes exist once, both paths keep working, and there is no
        "original" that can break. It **requires both paths on the same
        filesystem** — across mounts it simply fails, and that failure is the
        script telling you the truth.

        `ln -sf -- "$keeper" "$path"` (symlink) works across filesystems, but the
        trade-offs are real: the link breaks if the keeper is ever moved or
        deleted, backup and sync tools treat symlinks inconsistently, and a rescan
        sees a symlink rather than a file. If you want that, substitute it in the
        `hardlink` branch and accept that you now have a dependency graph to
        maintain. Hard links have their own caveat — editing *either* path edits
        the single set of bytes — so neither is free.

=== "macOS (zsh + jq)"

    Same logic; the differences are all BSD-vs-GNU userland. **`stat` is the
    important one:** macOS `stat` does not understand `-c`, so GNU's `stat -c%s`
    / `stat -c%Y` become **`stat -f%z`** (size) and **`stat -f%m`** (mtime).
    `base64 -d` becomes **`base64 -D`**. Install the dependencies with
    `brew install jq xxhash` (jq required, xxhash only for `--verify-hash`).

    ```zsh title="filearr-dedupe.zsh"
    #!/usr/bin/env zsh
    # Act on a Filearr duplicate_files_detail NDJSON export (macOS / BSD userland).
    # DRY RUN unless --execute is passed. Never touches keep_hint=="keep".
    set -eu
    setopt pipefail

    EXPORT=""; MODE="quarantine"; QDIR=""; EXECUTE=0
    ALLOW_QUICK=0; VERIFY_HASH=0; MTIME_TOLERANCE=2

    usage() {
      cat <<'USAGE'
    Usage: filearr-dedupe.zsh --export FILE [options]
      --export FILE        NDJSON export of duplicate_files_detail (required)
      --mode MODE          quarantine (default) | delete | hardlink
      --quarantine DIR     holding area for --mode quarantine (required for it)
      --execute            actually do it (default is a dry run)
      --allow-quick-hash   also act on sampled quick_hash groups (see the docs)
      --verify-hash        additionally require xxhsum agreement with the keeper
    USAGE
    }

    while [ $# -gt 0 ]; do
      case "$1" in
        --export)           EXPORT="$2"; shift 2 ;;
        --mode)             MODE="$2"; shift 2 ;;
        --quarantine)       QDIR="$2"; shift 2 ;;
        --execute)          EXECUTE=1; shift ;;
        --allow-quick-hash) ALLOW_QUICK=1; shift ;;
        --verify-hash)      VERIFY_HASH=1; shift ;;
        -h|--help)          usage; exit 0 ;;
        *) print -u2 "unknown argument: $1"; usage; exit 2 ;;
      esac
    done

    [ -n "$EXPORT" ] && [ -r "$EXPORT" ] || { print -u2 "--export FILE is required"; exit 2 }
    command -v jq >/dev/null || { print -u2 "jq is required (brew install jq)"; exit 2 }
    case "$MODE" in
      quarantine) [ -n "$QDIR" ] || { print -u2 "--quarantine DIR is required"; exit 2 } ;;
      delete|hardlink) ;;
      *) print -u2 "unknown --mode $MODE"; exit 2 ;;
    esac
    if [ "$VERIFY_HASH" -eq 1 ]; then
      command -v xxhsum >/dev/null || { print -u2 "--verify-hash needs xxhsum (brew install xxhash)"; exit 2 }
    fi

    JQ_PRELUDE='
      def to_epoch:
        sub("\\.[0-9]+"; "") | sub("Z$"; "+00:00")
        | capture("^(?<b>.{19})(?<sign>[+-])(?<hh>[0-9]{2}):?(?<mm>[0-9]{2})$") as $c
        | (($c.b + "Z") | fromdateiso8601)
          - ((if $c.sign == "-" then -1 else 1 end)
             * (($c.hh | tonumber) * 3600 + ($c.mm | tonumber) * 60));
      def best_path:
        (.native_path // "") as $n | (.share_unc // "") as $u
        | if ($n | length) > 0 then $n
          elif ($u | length) > 0 then $u
          else .path end;
    '

    typeset -A KEEP_B64 KEEP_SIZE
    while IFS=$'\t' read -r gk pb64 sz; do
      KEEP_B64[$gk]="$pb64"; KEEP_SIZE[$gk]="$sz"
    done < <(jq -r "$JQ_PRELUDE"'
      select(.keep_hint == "keep")
      | [ .group_key, (best_path | @base64), (.size | tostring) ] | @tsv
    ' "$EXPORT")

    acted=0; missing=0; mismatch=0; skipped_tier=0; reclaimed=0
    while IFS=$'\t' read -r gk pb64 sz mt tier copies; do
      path="$(printf '%s' "$pb64" | base64 -D)"    # BSD base64: -D, not -d

      if [ "$tier" != "content_hash" ] && [ "$ALLOW_QUICK" -eq 0 ]; then
        skipped_tier=$((skipped_tier + 1)); continue
      fi
      if [ ! -f "$path" ]; then
        print -u2 "SKIP  missing:       $path"; missing=$((missing + 1)); continue
      fi

      live_size=$(stat -f%z -- "$path")            # BSD stat: -f%z, not -c%s
      live_mtime=$(stat -f%m -- "$path")           # BSD stat: -f%m, not -c%Y
      if [ "$live_size" != "$sz" ]; then
        print -u2 "SKIP  size changed:  $path ($live_size != $sz)"
        mismatch=$((mismatch + 1)); continue
      fi
      delta=$(( live_mtime - mt )); [ "$delta" -lt 0 ] && delta=$(( -delta ))
      if [ "$delta" -gt "$MTIME_TOLERANCE" ]; then
        print -u2 "SKIP  mtime changed: $path"
        mismatch=$((mismatch + 1)); continue
      fi

      keeper=""
      if [ -n "${KEEP_B64[$gk]:-}" ]; then
        keeper="$(printf '%s' "${KEEP_B64[$gk]}" | base64 -D)"
      fi
      if [ "$MODE" = "hardlink" ] || [ "$VERIFY_HASH" -eq 1 ]; then
        if [ -z "$keeper" ] || [ ! -f "$keeper" ]; then
          print -u2 "SKIP  keeper missing (group $gk): $path"
          mismatch=$((mismatch + 1)); continue
        fi
        if [ "$(stat -f%z -- "$keeper")" != "${KEEP_SIZE[$gk]}" ]; then
          print -u2 "SKIP  keeper changed (group $gk): $path"
          mismatch=$((mismatch + 1)); continue
        fi
      fi
      if [ "$VERIFY_HASH" -eq 1 ]; then
        a=$(xxhsum -H2 -- "$keeper" | awk '{print $1}')
        b=$(xxhsum -H2 -- "$path"   | awk '{print $1}')
        if [ "$a" != "$b" ]; then
          print -u2 "SKIP  hash differs:  $path"
          mismatch=$((mismatch + 1)); continue
        fi
      fi

      case "$MODE" in
        delete)     action="delete           $path (1 of $copies)" ;;
        quarantine) dest="$QDIR/${path#/}"; action="quarantine       $path -> $dest" ;;
        hardlink)   action="hardlink to keep $path -> $keeper" ;;
      esac

      if [ "$EXECUTE" -eq 0 ]; then
        print "DRY   would $action"
      else
        case "$MODE" in
          delete)     rm -f -- "$path" ;;
          quarantine) mkdir -p -- "${dest:h}"; mv -- "$path" "$dest" ;;
          hardlink)   ln -f -- "$keeper" "$path" ;;   # same filesystem only
        esac
        print "DONE  $action"
      fi
      acted=$((acted + 1)); reclaimed=$((reclaimed + sz))
    done < <(jq -r "$JQ_PRELUDE"'
      select(.keep_hint == "candidate")
      | [ .group_key, (best_path | @base64), (.size | tostring),
          (.mtime | to_epoch | tostring), .hash_tier, (.copies_in_group | tostring) ]
      | @tsv
    ' "$EXPORT")

    if [ "$EXECUTE" -eq 1 ]; then verb="Acted on"; else verb="Would act on"; fi
    printf '\n%s: %d file(s), %d bytes (%s)\n' "$verb" "$acted" "$reclaimed" "$MODE"
    printf 'skipped: %d missing, %d changed since export, %d sampled-hash group(s)\n' \
      "$missing" "$mismatch" "$skipped_tier"
    [ "$EXECUTE" -eq 1 ] || printf 'This was a DRY RUN. Re-run with --execute.\n'
    ```

    !!! note "APFS, external drives, and `mv`"
        Hard links work on APFS and HFS+ but still only **within one volume** — an
        external drive and the internal SSD cannot share an inode, so `ln -f`
        fails there (correctly). Note also that macOS `mv` across volumes copies
        then deletes, so a quarantine directory on a different disk turns every
        move into a full read + write.

=== "Windows (PowerShell)"

    **No dependencies at all.** `Import-Csv` is native and parses quoted commas
    and embedded newlines correctly, so the CSV export is the natural input here
    (unlike the shells, where NDJSON + jq is the safe route). Paths come from
    `native_path`, falling back to `share_unc` — on Windows the UNC form is
    usually the one that resolves. The script also undoes the
    [CSV formula guard](#csv-formula-guard) before using a path.

    ```powershell title="Invoke-FilearrDedupe.ps1"
    <#
    .SYNOPSIS
      Act on a Filearr duplicate_files_detail CSV export.
    .DESCRIPTION
      DRY RUN unless -Execute is passed. Never touches keep_hint == 'keep'.
      Re-verifies size and mtime against the live file before acting; any
      disagreement skips that file (fail closed), because an export is a snapshot.
    #>
    [CmdletBinding()]
    param(
      [Parameter(Mandatory = $true)][string] $ExportPath,
      [ValidateSet('quarantine', 'delete', 'hardlink')][string] $Mode = 'quarantine',
      [string] $QuarantineDir,
      [switch] $Execute,
      [switch] $AllowQuickHash,
      [switch] $VerifyHash,
      [int] $MtimeToleranceSeconds = 2
    )

    $ErrorActionPreference = 'Stop'
    if ($Mode -eq 'quarantine' -and -not $QuarantineDir) {
      throw '-QuarantineDir is required for -Mode quarantine'
    }
    if ($VerifyHash -and -not (Get-Command xxhsum -ErrorAction SilentlyContinue)) {
      throw '-VerifyHash needs xxhsum on PATH (github.com/Cyan4973/xxHash/releases)'
    }

    # The CSV export is formula-injection guarded: a cell starting with = + - @
    # tab or CR is written with a leading apostrophe. Undo that, and only that.
    function Restore-GuardedCell([string] $Value) {
      if ($Value.Length -ge 2 -and $Value[0] -eq "'" -and
          "=+-@`t`r".Contains($Value[1])) { return $Value.Substring(1) }
      return $Value
    }

    # Documented resolution order: native_path -> share_unc -> path.
    function Resolve-RowPath($Row) {
      foreach ($col in 'native_path', 'share_unc', 'path') {
        $v = Restore-GuardedCell ([string] $Row.$col)
        if ($v) { return $v }
      }
      return $null
    }

    $rows = Import-Csv -LiteralPath $ExportPath

    # Pass 1: remember each group's keeper (hardlink / hash verify need it).
    $keepers = @{}
    foreach ($r in $rows | Where-Object { $_.keep_hint -eq 'keep' }) {
      $keepers[$r.group_key] = [pscustomobject]@{
        Path = Resolve-RowPath $r
        Size = [int64] $r.size
      }
    }

    $acted = 0; $missing = 0; $mismatch = 0; $skippedTier = 0; [int64] $reclaimed = 0

    foreach ($r in $rows | Where-Object { $_.keep_hint -eq 'candidate' }) {
      if ($r.hash_tier -ne 'content_hash' -and -not $AllowQuickHash) {
        $skippedTier++; continue
      }
      $path = Resolve-RowPath $r
      if (-not $path -or -not (Test-Path -LiteralPath $path -PathType Leaf)) {
        Write-Warning "SKIP  missing:       $path"; $missing++; continue
      }

      # Fail closed on ANY disagreement with the export snapshot.
      $file = Get-Item -LiteralPath $path -Force
      if ($file.Length -ne [int64] $r.size) {
        Write-Warning "SKIP  size changed:  $path ($($file.Length) != $($r.size))"
        $mismatch++; continue
      }
      $expected = [datetimeoffset]::Parse(
        $r.mtime, $null, [Globalization.DateTimeStyles]::RoundtripKind).UtcDateTime
      $skew = [math]::Abs(($file.LastWriteTimeUtc - $expected).TotalSeconds)
      if ($skew -gt $MtimeToleranceSeconds) {
        Write-Warning "SKIP  mtime changed: $path"; $mismatch++; continue
      }

      $keeper = $keepers[$r.group_key]
      if ($Mode -eq 'hardlink' -or $VerifyHash) {
        if (-not $keeper -or -not $keeper.Path -or
            -not (Test-Path -LiteralPath $keeper.Path -PathType Leaf)) {
          Write-Warning "SKIP  keeper missing (group $($r.group_key)): $path"
          $mismatch++; continue
        }
        if ((Get-Item -LiteralPath $keeper.Path -Force).Length -ne $keeper.Size) {
          Write-Warning "SKIP  keeper changed (group $($r.group_key)): $path"
          $mismatch++; continue
        }
      }
      if ($VerifyHash) {
        # Compare the two files to EACH OTHER (xxh128) -- never to a stored digest.
        $a = (& xxhsum -H2 -- $keeper.Path).Split(' ')[0]
        $b = (& xxhsum -H2 -- $path).Split(' ')[0]
        if ($a -ne $b) {
          Write-Warning "SKIP  hash differs:  $path"; $mismatch++; continue
        }
      }

      switch ($Mode) {
        'delete'     { $what = "delete           $path (1 of $($r.copies_in_group))" }
        'quarantine' {
          # Mirror the original path under the holding area: strip the drive root
          # or UNC leader so the tree stays reconstructable.
          $rel  = $path -replace '^[A-Za-z]:[\\/]+', '' -replace '^\\\\', ''
          $dest = Join-Path $QuarantineDir $rel
          $what = "quarantine       $path -> $dest"
        }
        'hardlink'   { $what = "hardlink to keep $path -> $($keeper.Path)" }
      }

      if (-not $Execute) {
        Write-Host "DRY   would $what"
      } else {
        switch ($Mode) {
          'delete'     { Remove-Item -LiteralPath $path -Force }
          'quarantine' {
            $dir = Split-Path -Parent $dest
            if (-not (Test-Path -LiteralPath $dir)) {
              New-Item -ItemType Directory -Path $dir -Force | Out-Null
            }
            Move-Item -LiteralPath $path -Destination $dest -Force
          }
          'hardlink'   {
            # A hard link cannot cross volumes and needs NTFS/ReFS -- if the copy
            # and the keeper live on different drives this throws, which is the
            # correct outcome. The candidate must be removed first: New-Item will
            # not overwrite an existing file with a link.
            Remove-Item -LiteralPath $path -Force
            New-Item -ItemType HardLink -Path $path -Target $keeper.Path | Out-Null
          }
        }
        Write-Host "DONE  $what"
      }
      $acted++; $reclaimed += [int64] $r.size
    }

    $verb = if ($Execute) { 'Acted on' } else { 'Would act on' }
    Write-Host ""
    Write-Host "${verb}: $acted file(s), $reclaimed bytes ($Mode)"
    Write-Host "skipped: $missing missing, $mismatch changed since export, $skippedTier sampled-hash group(s)"
    if (-not $Execute) { Write-Host 'This was a DRY RUN. Re-run with -Execute.' }
    ```

    Typical use:

    ```powershell
    $headers = @{ Authorization = "Bearer $env:FILEARR_API_KEY" }
    Invoke-WebRequest -Headers $headers -OutFile dups.csv `
      "http://filearr.example.com:8484/api/v1/reports/duplicate_files_detail?format=csv"

    .\Invoke-FilearrDedupe.ps1 -ExportPath dups.csv -QuarantineDir D:\quarantine
    .\Invoke-FilearrDedupe.ps1 -ExportPath dups.csv -QuarantineDir D:\quarantine -Execute
    ```

    !!! warning "Hard links on Windows"
        `New-Item -ItemType HardLink` requires **NTFS or ReFS and the same
        volume** — you cannot hard-link `C:\` to `D:\`, or anything to a network
        share. Creating a hard link does *not* require administrator rights
        (creating a **symbolic** link does, unless Developer Mode is on), which is
        why the script offers hard links and not symlinks.

### After you act {#after-acting}

Filearr will not notice the change until the affected library is rescanned. A
normal scan tombstones the removed files (`missing`, then the recycle-bin purge
after the retention window) — nothing is hard-deleted from the catalog, so a
mistake stays recoverable on the catalog side even when it is not on the disk
side. That is the other reason quarantine is the recommended first mode: rescan,
look at what the catalog now says, and only then empty the holding area.
