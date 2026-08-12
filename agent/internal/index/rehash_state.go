package index

// QH-T6 quick_hash migration cursor accessors (2026-08-12). The rehash_state
// singleton (schema.go) is a PURELY LOCAL table in the same sense extract_state
// and thumb_markers are: it is never replicated, carries no wire meaning, and
// exists only so the operator-triggered re-hash sweep is resumable and
// idempotent.
//
// Why a cursor is the ONLY bookkeeping available. There is no hash-provenance
// column anywhere in this schema, and there cannot cheaply be one — adding a
// column to `items` means an ALTER on a million-row store on every deployed
// agent, and central cannot supply the answer either: agentsync.apply_batch
// never writes policy_version for agent-owned rows, so central's cfg1->cfg2
// scheme (the equivalent provenance marker for its OWN rows) has no jurisdiction
// here. Neither side can look at a stored quick_hash and say whether it was
// produced before or after the QH-T1 fix. What this table records instead is
// "this agent swept this band under this hash scheme, and got this far" — which
// is a weaker claim, and the strongest one obtainable.

import (
	"context"
	"database/sql"
	"fmt"
)

// RehashState is the rehash_state singleton row. FP is the SWEEP fingerprint
// (rehash.Fingerprint: hash scheme version + the size band), so a repeat command
// at the same scheme and band is a no-op while a band change or a future hashing
// change invalidates the cursor. StartedAt/FinishedAt are RFC3339 text (the
// store's timestamp convention, helpers.go tsText) with "" standing in for SQL
// NULL — "" in FinishedAt is what "a sweep is still incomplete" means, and it is
// the flag the resume path keys on.
//
// The counters are CUMULATIVE over one sweep (which an operator may run in
// several MaxItems-bounded chunks), not per command.
//
// Changed and Verified are SEPARATE counters rather than one "processed" total,
// and that split is the point of the whole report: Verified is a file the sweep
// re-read and found already correct (an ordinary rescan had touched it since the
// QH-T1 binary landed, so the changed-file path had already repaired it), while
// Changed is a stale row this sweep actually corrected. An operator watching
// verified climb and changed stay flat is watching the sweep confirm work that
// was already done — which looks identical to "the sweep is broken" if the two
// are added together.
//
// MinSize/MaxSize are the band this cursor was walking. A cursor without its
// band is meaningless: rowid 400000 under the 65537..131072 default and rowid
// 400000 under a wider opt-in backfill have covered entirely different sets of
// files, so the band is part of the fingerprint AND stored in plain integers
// here so the health block can report it without parsing the fingerprint.
type RehashState struct {
	FP          string
	CursorRowID int64
	StartedAt   string // RFC3339, "" when never started
	FinishedAt  string // RFC3339, "" while a sweep is incomplete

	Seen, Changed, Verified, Skipped, Failed int64

	MinSize, MaxSize int64
}

// RehashState reads the singleton row. The schema seeds it with INSERT OR
// IGNORE, so a missing row is not an error condition the caller has to think
// about — a fresh (or somehow rowless) store reads back the zero value, which is
// exactly "no sweep has ever run here".
func (s *Store) RehashState(ctx context.Context) (RehashState, error) {
	var (
		st         RehashState
		startedAt  sql.NullString
		finishedAt sql.NullString
	)
	err := s.db.QueryRowContext(ctx, `
SELECT fp, cursor_rowid, started_at, finished_at,
       seen, changed, verified, skipped, failed, min_size, max_size
FROM rehash_state WHERE id = 1`,
	).Scan(&st.FP, &st.CursorRowID, &startedAt, &finishedAt,
		&st.Seen, &st.Changed, &st.Verified, &st.Skipped, &st.Failed,
		&st.MinSize, &st.MaxSize)
	if err == sql.ErrNoRows {
		return RehashState{}, nil
	}
	if err != nil {
		return RehashState{}, fmt.Errorf("read rehash state: %w", err)
	}
	st.StartedAt = startedAt.String
	st.FinishedAt = finishedAt.String
	return st, nil
}

// SaveRehashState persists the row on its own connection. Used for the one-off
// write that stands outside a batch (stamping a fresh fingerprint before the
// sweep begins); the per-batch cursor advance uses SaveRehashStateTx instead.
func (s *Store) SaveRehashState(ctx context.Context, st RehashState) error {
	if _, err := s.db.ExecContext(ctx, saveRehashStateSQL, saveRehashStateArgs(st)...); err != nil {
		return fmt.Errorf("save rehash state: %w", err)
	}
	return nil
}

// SaveRehashStateTx persists the row inside a CALLER-OWNED transaction, so the
// cursor, the corrected item rows and the outbox events they produced all commit
// atomically. This matters more here than it does for the re-extract sweep: that
// one only ever wrote events, while this one also REWRITES the item rows. A
// cursor that advanced past an UpdateItem which then rolled back would leave the
// local index holding the stale hash with nothing left to revisit it, and the
// sweep would report the band as migrated.
func SaveRehashStateTx(ctx context.Context, tx *sql.Tx, st RehashState) error {
	if _, err := tx.ExecContext(ctx, saveRehashStateSQL, saveRehashStateArgs(st)...); err != nil {
		return fmt.Errorf("save rehash state: %w", err)
	}
	return nil
}

// saveRehashStateSQL is an upsert rather than an UPDATE so the write self-heals a
// store whose seed row is somehow absent; id is CHECK-constrained to 1, which
// keeps "singleton" a schema guarantee and not a convention.
const saveRehashStateSQL = `
INSERT INTO rehash_state(id, fp, cursor_rowid, started_at, finished_at,
                         seen, changed, verified, skipped, failed, min_size, max_size)
VALUES(1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
    fp           = excluded.fp,
    cursor_rowid = excluded.cursor_rowid,
    started_at   = excluded.started_at,
    finished_at  = excluded.finished_at,
    seen         = excluded.seen,
    changed      = excluded.changed,
    verified     = excluded.verified,
    skipped      = excluded.skipped,
    failed       = excluded.failed,
    min_size     = excluded.min_size,
    max_size     = excluded.max_size`

func saveRehashStateArgs(st RehashState) []any {
	return []any{
		st.FP, st.CursorRowID, nullStr(st.StartedAt), nullStr(st.FinishedAt),
		st.Seen, st.Changed, st.Verified, st.Skipped, st.Failed,
		st.MinSize, st.MaxSize,
	}
}

// RehashCandidate is one indexed item the sweep may re-hash.
//
// Unlike ExtractCandidate — which carries a flat copy of the identity fields
// because the re-extract sweep RE-EMITS them verbatim and never writes the row —
// this carries the WHOLE Item. The sweep's entire purpose is to overwrite the
// identity fields, and the only supported way to write an item row is
// index.UpdateItem, which takes a full *Item: it allocates the local sequence
// number, keeps the FTS triggers coherent, and is the one place the mutable
// column set is written down. Passing a partially-populated Item to it would
// blank every column this sweep does not care about.
//
// RootPath is the join to roots, needed to rebuild the absolute path (and the
// event's library_ref) the same way ExtractCandidate does.
type RehashCandidate struct {
	RowID    int64
	RootPath string
	Item     *Item
}

// rehashCandidateSQL is the candidate projection. The item columns are
// i.-qualified copies of selectItemColumns IN THE SAME ORDER, because scanItem
// consumes that order positionally; they cannot simply reuse the constant
// because joining roots makes the bare `id` and `path` columns ambiguous.
//
// Filters, all deliberate:
//
//   - rowid > ? — the resume frontier. rowid is the ordering key (not rel_path)
//     for the same reason the re-extract sweep uses it: SQLite rowids are
//     assigned on insert and never reshuffled, so a scan running concurrently
//     with the sweep appends new items ABOVE the cursor and the walk can neither
//     skip nor revisit rows it has already passed. Those new rows were hashed by
//     the current (fixed) binary anyway.
//   - status = 'active' — a missing/trashed row is a tombstone (invariant 4).
//     Central has been told the file is gone; re-emitting a modified event for it
//     would resurrect it, and its bytes are very likely not there to hash.
//   - is_sidecar = 0 — sidecars are never hashed by the scan in the first place
//     (scan.diffEntry skips hashing for them), so there is no stale hash to fix
//     and re-emitting them would make this path produce rows a full rescan never
//     would.
//   - size BETWEEN ? AND ? — the defect band. The old QuickHash read a fixed
//     64 KiB head unconditionally, so a file <= 65536 bytes had its ENTIRE
//     content hashed and is ALREADY CORRECT; only 65537..131072 lost its middle
//     and tail. Everything above 131072 was sampled head+tail then and is sampled
//     head+tail now — unchanged by QH-T1, and re-reading it would be pure waste.
//
// The existing partial index idx_items_rpt_size (size) WHERE status='active' AND
// is_sidecar=0 covers this shape; no new index is added.
const rehashCandidateSQL = `
SELECT i.rowid, r.path,
       i.id, i.root_id, i.rel_path, i.filename, i.extension, i.size, i.mtime_ns,
       i.quick_hash, i.content_hash, i.file_category, i.file_group, i.meta, i.status, i.is_sidecar,
       i.sidecar_of, i.first_seen, i.last_seen, i.synced_at, i.local_seq_no
FROM items i JOIN roots r ON r.id = i.root_id
WHERE i.rowid > ? AND i.status = ? AND i.is_sidecar = 0
  AND i.size IS NOT NULL AND i.size BETWEEN ? AND ?
ORDER BY i.rowid LIMIT ?`

// RehashCandidates returns up to limit ACTIVE, non-sidecar items with
// rowid > after whose size falls inside [minSize, maxSize], ordered by rowid.
func (s *Store) RehashCandidates(
	ctx context.Context, after, minSize, maxSize int64, limit int,
) ([]RehashCandidate, error) {
	if limit <= 0 {
		// An unbounded read of a million-row table is exactly what this iterator
		// exists to prevent; a caller asking for nothing gets nothing.
		return nil, nil
	}
	rows, err := s.db.QueryContext(ctx, rehashCandidateSQL, after, StatusActive, minSize, maxSize, limit)
	if err != nil {
		return nil, fmt.Errorf("query rehash candidates: %w", err)
	}
	defer rows.Close()
	out := make([]RehashCandidate, 0, limit)
	for rows.Next() {
		var c RehashCandidate
		// The two leading columns are consumed here and the rest is handed to the
		// shared scanItem, so the Item materialisation (NULL mapping, timestamp
		// parsing) stays in exactly one place.
		it, err := scanItem(prefixScanner{rows: rows, head: []any{&c.RowID, &c.RootPath}})
		if err != nil {
			return nil, err
		}
		c.Item = it
		out = append(out, c)
	}
	return out, rows.Err()
}

// prefixScanner adapts a row that carries EXTRA leading columns to the
// rowScanner scanItem expects: it prepends its own destinations to whatever
// scanItem asks for. Cheaper and far less brittle than a second copy of
// scanItem's twenty-column NULL-mapping body, which is the only other way to
// select a joined column alongside the canonical item projection.
type prefixScanner struct {
	rows rowScanner
	head []any
}

func (p prefixScanner) Scan(dest ...any) error {
	all := make([]any, 0, len(p.head)+len(dest))
	all = append(all, p.head...)
	all = append(all, dest...)
	return p.rows.Scan(all...)
}
