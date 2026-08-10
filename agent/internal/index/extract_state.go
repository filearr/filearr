package index

// Re-extraction cursor accessors (agent parity phase 3). The extract_state
// singleton (schema.go) is a PURELY LOCAL table in the same sense thumb_markers
// is: it is never replicated, carries no wire meaning, and exists only so an
// operator-triggered sweep of the existing index is resumable and idempotent.

import (
	"context"
	"database/sql"
	"fmt"
)

// ExtractState is the extract_state singleton row. FP is the extraction
// CONFIGURATION fingerprint the sweep last ran under (reextract.Fingerprint), so
// a repeat command at the same configuration is a no-op while a policy change or
// a newly installed host tool invalidates the cursor. StartedAt/FinishedAt are
// RFC3339 text (the store's timestamp convention, see helpers.go tsText) with ""
// standing in for SQL NULL — "" in FinishedAt is what "a sweep is still
// incomplete" means, and it is the flag the resume path keys on.
//
// The counters are CUMULATIVE over one sweep (which an operator may run in
// several MaxItems-bounded chunks), not per command, so the row answers "what
// did this sweep do in total".
type ExtractState struct {
	FP          string
	CursorRowID int64
	StartedAt   string // RFC3339, "" when never started
	FinishedAt  string // RFC3339, "" while a sweep is incomplete

	Seen, Extracted, Skipped, Failed int64
}

// ExtractState reads the singleton row. The schema seeds it with INSERT OR
// IGNORE, so a missing row is not an error condition the caller has to think
// about — a fresh (or somehow rowless) store reads back the zero value, which is
// exactly "no sweep has ever run here".
func (s *Store) ExtractState(ctx context.Context) (ExtractState, error) {
	var (
		st         ExtractState
		startedAt  sql.NullString
		finishedAt sql.NullString
	)
	err := s.db.QueryRowContext(ctx, `
SELECT fp, cursor_rowid, started_at, finished_at, seen, extracted, skipped, failed
FROM extract_state WHERE id = 1`,
	).Scan(&st.FP, &st.CursorRowID, &startedAt, &finishedAt,
		&st.Seen, &st.Extracted, &st.Skipped, &st.Failed)
	if err == sql.ErrNoRows {
		return ExtractState{}, nil
	}
	if err != nil {
		return ExtractState{}, fmt.Errorf("read extract state: %w", err)
	}
	st.StartedAt = startedAt.String
	st.FinishedAt = finishedAt.String
	return st, nil
}

// SaveExtractState persists the row on its own connection. Used for the one-off
// writes that stand outside a batch (stamping a fresh fingerprint before the
// sweep begins); the per-batch cursor advance uses SaveExtractStateTx instead.
func (s *Store) SaveExtractState(ctx context.Context, st ExtractState) error {
	_, err := s.db.ExecContext(ctx, saveExtractStateSQL, saveExtractStateArgs(st)...)
	if err != nil {
		return fmt.Errorf("save extract state: %w", err)
	}
	return nil
}

// SaveExtractStateTx persists the row inside a CALLER-OWNED transaction, so the
// cursor and the outbox events it covers commit atomically — the same discipline
// outbox.Write imposes on the scan (a rolled-back batch must leave neither the
// events nor a cursor claiming they were emitted). Without this the sweep could
// advance past items whose events never landed, and those items would be skipped
// for the rest of the sweep with nothing to show for it.
func SaveExtractStateTx(ctx context.Context, tx *sql.Tx, st ExtractState) error {
	_, err := tx.ExecContext(ctx, saveExtractStateSQL, saveExtractStateArgs(st)...)
	if err != nil {
		return fmt.Errorf("save extract state: %w", err)
	}
	return nil
}

// saveExtractStateSQL is an upsert rather than an UPDATE so the write self-heals
// a store whose seed row is somehow absent; id is CHECK-constrained to 1, which
// keeps "singleton" a schema guarantee and not a convention.
const saveExtractStateSQL = `
INSERT INTO extract_state(id, fp, cursor_rowid, started_at, finished_at, seen, extracted, skipped, failed)
VALUES(1, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
    fp           = excluded.fp,
    cursor_rowid = excluded.cursor_rowid,
    started_at   = excluded.started_at,
    finished_at  = excluded.finished_at,
    seen         = excluded.seen,
    extracted    = excluded.extracted,
    skipped      = excluded.skipped,
    failed       = excluded.failed`

func saveExtractStateArgs(st ExtractState) []any {
	return []any{
		st.FP, st.CursorRowID, nullStr(st.StartedAt), nullStr(st.FinishedAt),
		st.Seen, st.Extracted, st.Skipped, st.Failed,
	}
}

// ExtractCandidate is one indexed item the sweep may re-extract, joined to its
// root so the absolute path (and the event's library_ref) can be reconstructed.
// The identity fields are carried verbatim because the sweep RE-EMITS them
// unchanged: it re-extracts a file whose bytes it has already confirmed are the
// same, so re-deriving size/hashes would be pointless work and re-deriving them
// WRONG would push bad identity to central.
type ExtractCandidate struct {
	RowID        int64
	ID           string
	RootPath     string
	RelPath      string
	FileCategory string
	Size         int64
	MtimeNs      int64
	QuickHash    string
	ContentHash  string
}

// ExtractCandidates returns up to limit ACTIVE items with rowid > after, ordered
// by rowid.
//
// Two filters, both deliberate:
//
//   - status = 'active' only. A missing/trashed row is a tombstone (invariant 4);
//     central has been told the file is gone, and re-emitting a modified event
//     for it would resurrect it from stale identity.
//   - is_sidecar = 0, mirroring scan.diffEntry, which skips extraction for
//     sidecars because they are pointers to a primary item rather than content in
//     their own right. Extracting them here would make the sweep produce metadata
//     a full rescan never would, i.e. leave the two paths permanently divergent.
//
// rowid is the ordering key (not rel_path) because SQLite rowids are assigned on
// insert and never reshuffled: a scan running concurrently with the sweep appends
// new items ABOVE the cursor, so the walk can neither skip nor revisit rows it
// has already passed. size/mtime_ns are COALESCEd because the columns are
// nullable; a NULL reads back as 0, which the sweep's stat comparison then treats
// as "changed on disk" and skips — degrading to a no-op rather than an error that
// would abort the whole sweep.
func (s *Store) ExtractCandidates(ctx context.Context, after int64, limit int) ([]ExtractCandidate, error) {
	if limit <= 0 {
		// An unbounded read of a million-row table is exactly what this iterator
		// exists to prevent; a caller asking for nothing gets nothing.
		return nil, nil
	}
	rows, err := s.db.QueryContext(ctx, `
SELECT i.rowid, i.id, r.path, i.rel_path, COALESCE(i.file_category, ''),
       COALESCE(i.size, 0), COALESCE(i.mtime_ns, 0),
       COALESCE(i.quick_hash, ''), COALESCE(i.content_hash, '')
FROM items i JOIN roots r ON r.id = i.root_id
WHERE i.rowid > ? AND i.status = ? AND i.is_sidecar = 0
ORDER BY i.rowid LIMIT ?`, after, StatusActive, limit)
	if err != nil {
		return nil, fmt.Errorf("query extract candidates: %w", err)
	}
	defer rows.Close()
	out := make([]ExtractCandidate, 0, limit)
	for rows.Next() {
		var c ExtractCandidate
		if err := rows.Scan(&c.RowID, &c.ID, &c.RootPath, &c.RelPath, &c.FileCategory,
			&c.Size, &c.MtimeNs, &c.QuickHash, &c.ContentHash); err != nil {
			return nil, err
		}
		out = append(out, c)
	}
	return out, rows.Err()
}
