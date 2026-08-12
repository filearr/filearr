package index

import (
	"database/sql"
	"fmt"
)

// schemaVersion is stamped into PRAGMA user_version so a future migration can
// detect and upgrade an older store. v2 adds the P5-T4 replication outbox; v3
// adds the store_flags table (the durable rebuilt marker, P5-T5); v4 adds the
// P12-T13 thumb_markers table (local-only thumbnail generation cursor); v5
// (W8-E) replaces the static media_type column with the File Extension
// Similarity Taxonomy pair file_category + file_group. No in-place migration —
// an older store fails integrity/version and is rebuilt from a fresh walk
// (disposable-index philosophy, invariant 1), which re-classifies every item
// against the live taxonomy.
//
// READ THIS BEFORE BUMPING IT. The version is not a schema serial number; it is
// a DESTRUCTIVE signal. integrity.go:schemaOutdated deletes any store stamped
// below the constant and Open recreates it empty, so a bump costs every deployed
// agent a full re-walk and a re-emission of its whole index (~1.09M items on the
// live agent, plus a Meili sync job per applied batch centrally). Bump it ONLY
// when an EXISTING column's meaning changes and a stale row would be actively
// wrong. Purely additive local tables — thumb_markers, extract_state, and
// rehash_state (QH-T6, 2026-08-12) — ride CREATE TABLE IF NOT EXISTS in
// schemaSQL, which migrate() applies in place on the next open. That is the
// upgrade path, and it is why the table added below did NOT take a v6.
const schemaVersion = 5

// schemaSQL is the full DDL. The items table mirrors a narrow subset of central
// items (agent/docs/layout.md): identity is (root_id, rel_path). mtime_ns is
// INTEGER Unix nanoseconds (ruling 2). The FTS5 external-content table over
// filename+rel_path uses the trigram tokenizer (ruling 1: chosen now so P7's
// trigram-MATCH query surface needs no schema rebuild). local_seq_no + synced_at
// are the local-only replication cursor columns.
const schemaSQL = `
CREATE TABLE IF NOT EXISTS roots (
    id       TEXT PRIMARY KEY,
    path     TEXT NOT NULL UNIQUE,
    added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
    id           TEXT PRIMARY KEY,
    root_id      TEXT NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
    rel_path     TEXT NOT NULL,
    filename     TEXT NOT NULL,
    extension    TEXT,
    size         INTEGER,
    mtime_ns     INTEGER,
    quick_hash    TEXT,
    content_hash  TEXT,
    file_category TEXT,
    file_group    TEXT,
    meta          TEXT,
    status       TEXT NOT NULL,
    is_sidecar   INTEGER NOT NULL DEFAULT 0,
    sidecar_of   TEXT,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    synced_at    TEXT,
    local_seq_no INTEGER NOT NULL DEFAULT 0,
    UNIQUE(root_id, rel_path)
);

CREATE INDEX IF NOT EXISTS idx_items_root_status ON items(root_id, status);
CREATE INDEX IF NOT EXISTS idx_items_quick       ON items(root_id, quick_hash, size);
CREATE INDEX IF NOT EXISTS idx_items_unsynced    ON items(synced_at, local_seq_no);

CREATE TABLE IF NOT EXISTS local_meta (
    id       INTEGER PRIMARY KEY CHECK (id = 1),
    next_seq INTEGER NOT NULL
);
INSERT OR IGNORE INTO local_meta(id, next_seq) VALUES(1, 0);

CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    filename, rel_path,
    content='items', content_rowid='rowid',
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
    INSERT INTO items_fts(rowid, filename, rel_path)
    VALUES (new.rowid, new.filename, new.rel_path);
END;
CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, filename, rel_path)
    VALUES ('delete', old.rowid, old.filename, old.rel_path);
END;
CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, filename, rel_path)
    VALUES ('delete', old.rowid, old.filename, old.rel_path);
    INSERT INTO items_fts(rowid, filename, rel_path)
    VALUES (new.rowid, new.filename, new.rel_path);
END;

-- P5-T4 transactional outbox. Each row is one AgentEvent (backend
-- filearr/agentsync.py wire contract) written IN THE SAME *sql.Tx as the item
-- mutation that produced it, so a rolled-back scan batch leaves neither the item
-- change nor its event. seq_no is AUTOINCREMENT (never reused, even after a row
-- is marked sent) and IS the durable wire seq_no the central seq-gap guard keys
-- on — the items.local_seq_no column stays a purely local bookkeeping cursor and
-- no longer feeds the wire (agent/internal/outbox docs the unification). payload
-- is the AgentEvent JSON minus seq_no; the drain injects seq_no from this column.
CREATE TABLE IF NOT EXISTS outbox (
    seq_no     INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id    TEXT NOT NULL,
    op         TEXT NOT NULL,
    payload    TEXT NOT NULL,
    written_at TEXT NOT NULL,
    sent_at    TEXT,
    batch_id   TEXT
);
CREATE INDEX IF NOT EXISTS ix_outbox_unsent ON outbox(seq_no) WHERE sent_at IS NULL;

-- 2026-07-28 local-report indexes (web UI Reports tab). Full-table aggregates
-- over a million-row items table take ~10s+ per query in pure-Go SQLite (live
-- report: the Reports tab "returned no data" — it was timing out); these
-- narrow partial indexes make the report GROUP BYs stream index-only instead.
-- Applied idempotently at open: an existing large DB pays a one-time build.
CREATE INDEX IF NOT EXISTS idx_items_rpt_category ON items(file_category, size)
    WHERE status='active';
-- Interactive kind:-only queries (search category chips with an empty text
-- box): the searcher's filter-only path is WHERE file_category=? ORDER BY
-- rel_path LIMIT n, which without this index sorts EVERY match (or walks the
-- whole table for a rare category) before the LIMIT can stop it. This
-- composite provides both the equality and the order, so the query streams
-- and stops at the limit. NOT partial: the searcher WHERE carries no status
-- predicate, so a partial index would never be chosen.
CREATE INDEX IF NOT EXISTS idx_items_kind_path ON items(file_category, rel_path);
CREATE INDEX IF NOT EXISTS idx_items_rpt_size ON items(size)
    WHERE status='active' AND is_sidecar=0;
CREATE INDEX IF NOT EXISTS idx_items_rpt_chash ON items(content_hash, size)
    WHERE status='active' AND is_sidecar=0 AND size>0
      AND content_hash IS NOT NULL AND content_hash!='';
CREATE INDEX IF NOT EXISTS idx_items_rpt_qhash ON items(quick_hash, size)
    WHERE status='active' AND is_sidecar=0 AND size>0
      AND (content_hash IS NULL OR content_hash='')
      AND quick_hash IS NOT NULL AND quick_hash!='';

-- P5-T5 durable store flags. A key/value scratch table that SURVIVES a process
-- restart (unlike Store.Rebuilt, an in-memory field). Open writes
-- rebuilt_pending=1 whenever it fresh-creates OR corruption-rebuilds the
-- database, so a LATER process (e.g. a scan rebuilds, then a separate
-- reconcile run) still knows the local seq base was reset and must send
-- rebuilt=true so central resets its per-agent watermark -- otherwise fresh low
-- seq_no rows are silently fast-forwarded away as stale (no apply). The
-- reconcile sweep clears it only after a successful rebuilt-carrying sweep.
CREATE TABLE IF NOT EXISTS store_flags (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);

-- P12-T13 thumbnail generation cursor. Records which (item, tier) thumbnails the
-- agent has already generated + uploaded to central, keyed on the content-address
-- cache_key. This is a PURELY LOCAL table: it is NEVER replicated (no outbox row,
-- not in the AgentEvent payload) — central owns the authoritative thumbnail_manifest.
-- A stored cache_key that differs from the freshly-computed expected key (a changed
-- file → new hash → new key, or a GeneratorVersion bump) means "regenerate". The FK
-- CASCADE drops markers when their item is deleted so the table self-cleans.
CREATE TABLE IF NOT EXISTS thumb_markers (
    item_id     TEXT NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    tier        INTEGER NOT NULL,
    cache_key   TEXT NOT NULL,
    uploaded_at TEXT NOT NULL,
    PRIMARY KEY (item_id, tier)
);

-- Re-extraction cursor (agent parity phase 3). Extraction runs inside the scan,
-- over the files THAT scan reports as new or changed, so an item catalogued
-- before extraction was enabled — or before its host gained ffprobe/exiftool/
-- poppler — keeps identity-only metadata forever. The reextract command sweeps
-- the existing index and re-emits those items; this singleton row is what makes
-- the sweep resumable (cursor_rowid) and idempotent (fp = the extraction
-- CONFIGURATION it last completed under, so a repeat run at the same
-- configuration is a no-op unless forced).
--
-- Deliberately does NOT bump schemaVersion. A version bump means "rebuild the
-- index from a fresh walk" (invariant 1, disposable index), which for an
-- additive, local-only, empty-on-create table would cost a full re-walk and a
-- re-emission of every item on every deployed agent — a catastrophic price for a
-- cursor. CREATE TABLE IF NOT EXISTS in this DDL is applied to existing stores
-- by migrate() on the next open, which is exactly the cheap additive upgrade
-- this needs. Bump the version only when an EXISTING column's meaning changes.
CREATE TABLE IF NOT EXISTS extract_state (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    fp           TEXT NOT NULL DEFAULT '',
    cursor_rowid INTEGER NOT NULL DEFAULT 0,
    started_at   TEXT,
    finished_at  TEXT,
    seen         INTEGER NOT NULL DEFAULT 0,
    extracted    INTEGER NOT NULL DEFAULT 0,
    skipped      INTEGER NOT NULL DEFAULT 0,
    failed       INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO extract_state(id) VALUES(1);

-- QH-T6 quick_hash migration cursor (2026-08-12). QH-T1 (2026-07-18) fixed a
-- defect where a file in the 64-128 KiB band had its middle and tail silently
-- UNhashed: the old code read a fixed 64 KiB head and only added the tail above
-- 131072 bytes, so two different files whose first 64 KiB coincided produced the
-- same quick_hash — false duplicates, and a mis-keyed move-detection tier. The
-- fix is retroactive for nobody: scan.diffEntry re-hashes ONLY on a size/mtime
-- change or an empty quick_hash, so a stable file in that band keeps its wrong
-- hash forever. Central cannot repair these rows either — it does not host the
-- files, and agentsync.apply_batch never writes policy_version, so central's
-- own cfg1->cfg2 provenance sweep has no jurisdiction over agent-owned rows.
-- 98,628 rows across seven libraries were affected on the live fleet at the time
-- this shipped. The rehash_sweep command re-reads those files and emits the
-- corrected hashes; this singleton row is the ONLY bookkeeping that exists for
-- it, which is what makes the sweep resumable (cursor_rowid) and idempotent
-- (fp = hash scheme + band it last ran under).
--
-- Deliberately does NOT bump schemaVersion, for exactly the reason extract_state
-- does not (see above and integrity.go:schemaOutdated): a version bump DELETES
-- the store and rebuilds it from a fresh walk. For an additive, local-only,
-- empty-on-create cursor table that would cost every deployed agent a full
-- re-walk and a re-emission of its entire index — the ~1.09M-item live agent
-- included — to gain twelve columns of bookkeeping. CREATE TABLE IF NOT EXISTS
-- here is applied to existing stores by migrate() on the next open, which IS the
-- in-place upgrade path this needs. min_size/max_size are recorded alongside the
-- counters because the band is overridable per run: a cursor is meaningless
-- without the band it was walking.
CREATE TABLE IF NOT EXISTS rehash_state (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    fp           TEXT NOT NULL DEFAULT '',
    cursor_rowid INTEGER NOT NULL DEFAULT 0,
    started_at   TEXT,
    finished_at  TEXT,
    seen         INTEGER NOT NULL DEFAULT 0,
    changed      INTEGER NOT NULL DEFAULT 0,
    verified     INTEGER NOT NULL DEFAULT 0,
    skipped      INTEGER NOT NULL DEFAULT 0,
    failed       INTEGER NOT NULL DEFAULT 0,
    min_size     INTEGER NOT NULL DEFAULT 0,
    max_size     INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO rehash_state(id) VALUES(1);
`

// migrate applies the schema idempotently and stamps the version.
func migrate(db *sql.DB) error {
	if _, err := db.Exec(schemaSQL); err != nil {
		return fmt.Errorf("apply schema: %w", err)
	}
	if _, err := db.Exec(fmt.Sprintf("PRAGMA user_version = %d", schemaVersion)); err != nil {
		return fmt.Errorf("stamp schema version: %w", err)
	}
	return nil
}
