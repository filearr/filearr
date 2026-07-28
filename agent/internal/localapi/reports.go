package localapi

import (
	"context"
	"database/sql"
	"sort"
	"sync"
	"time"
)

// Local reporting (user request 2026-07-27): the agent-side counterpart of the
// console's canned Reports page, computed over the LOCAL index only.
//
// Scale posture (live report 2026-07-28, 1.7M-row index): every report is
// backed by the idx_items_rpt_* partial indexes (index/schema.go) so its
// aggregate streams index-only, computes the FULL result once (capped at
// reportCap rows) under a wall-clock budget, and serves pages/CSV from a
// short-lived in-process cache — first load does the work, everything after
// is instant. All SQL is code-controlled with bound parameters; report ids
// resolve via the registry, never into SQL.

// ReportSpec describes one canned report for the list endpoint.
type ReportSpec struct {
	ID          string   `json:"id"`
	Title       string   `json:"title"`
	Description string   `json:"description"`
	Columns     []string `json:"columns"`
	// ByteCols marks columns the UI right-aligns and renders human-sized.
	ByteCols []string `json:"byte_cols,omitempty"`
}

// ReportPage is one page of report rows.
type ReportPage struct {
	Spec   ReportSpec `json:"spec"`
	Rows   [][]any    `json:"rows"`
	Total  int        `json:"total"`
	Limit  int        `json:"limit"`
	Offset int        `json:"offset"`
	// Capped marks a result truncated at the compute cap — Total is a floor.
	Capped bool `json:"capped,omitempty"`
	// ComputedAt is when this result was (re)computed; pages are served from a
	// cache with reportCacheTTL freshness.
	ComputedAt string `json:"computed_at,omitempty"`
}

// reportCap bounds any report's materialized row count; reportCacheTTL bounds
// staleness; reportTimeout bounds one compute.
const (
	reportCap      = 10000
	reportCacheTTL = 5 * time.Minute
	reportTimeout  = 120 * time.Second
)

// futureSlackNs mirrors central's bad_mtime report: flag mtimes >48h ahead.
const futureSlackNs = int64(48 * time.Hour)

type reportDef struct {
	spec    ReportSpec
	compute func(ctx context.Context, db *sql.DB, now func() time.Time) (rows [][]any, capped bool, err error)
}

var reportDefs = []reportDef{
	{
		spec: ReportSpec{
			ID: "categories", Title: "Categories",
			Description: "Active items and bytes per taxonomy category.",
			Columns:     []string{"category", "files", "total_bytes"},
			ByteCols:    []string{"total_bytes"},
		},
		compute: computeCategories,
	},
	{
		spec: ReportSpec{
			ID: "unmapped_extensions", Title: "Unmapped extensions",
			Description: "Extensions the taxonomy does not classify, by file count.",
			Columns:     []string{"extension", "files", "total_bytes"},
			ByteCols:    []string{"total_bytes"},
		},
		compute: computeUnmapped,
	},
	{
		spec: ReportSpec{
			ID: "largest_files", Title: "Largest files",
			Description: "Active items by size, descending (top 10,000).",
			Columns:     []string{"path", "category", "size"},
			ByteCols:    []string{"size"},
		},
		compute: computeLargest,
	},
	{
		spec: ReportSpec{
			ID: "duplicate_files", Title: "Duplicate files",
			Description: "Hash groups with more than one copy (content hash when known, else sampled quick hash + size — a strong but not byte-certain signal). Largest wasted bytes first.",
			Columns:     []string{"tier", "copies", "wasted_bytes", "sample_path"},
			ByteCols:    []string{"wasted_bytes"},
		},
		compute: computeDuplicates,
	},
	{
		spec: ReportSpec{
			ID: "future_dated", Title: "Future-dated files",
			Description: "Items whose modification time is more than 48 hours in the future (clock or filesystem damage).",
			Columns:     []string{"path", "mtime", "size"},
			ByteCols:    []string{"size"},
		},
		compute: computeFutureDated,
	},
}

// computeCategories GROUPs BY the raw column so the scan streams the
// idx_items_rpt_category partial index; ''/NULL merge into "(unclassified)"
// here in Go (an expression group key would force SQLite into a full sort).
func computeCategories(ctx context.Context, db *sql.DB, _ func() time.Time) ([][]any, bool, error) {
	rows, err := db.QueryContext(ctx, `
		SELECT file_category, COUNT(*), COALESCE(SUM(size),0)
		FROM items WHERE status='active'
		GROUP BY file_category`)
	if err != nil {
		return nil, false, err
	}
	defer rows.Close()
	type agg struct{ files, bytes int64 }
	merged := map[string]*agg{}
	for rows.Next() {
		var cat sql.NullString
		var n, b int64
		if err := rows.Scan(&cat, &n, &b); err != nil {
			return nil, false, err
		}
		key := cat.String
		if !cat.Valid || key == "" {
			key = "(unclassified)"
		}
		a := merged[key]
		if a == nil {
			a = &agg{}
			merged[key] = a
		}
		a.files += n
		a.bytes += b
	}
	if err := rows.Err(); err != nil {
		return nil, false, err
	}
	out := make([][]any, 0, len(merged))
	for k, a := range merged {
		out = append(out, []any{k, a.files, a.bytes})
	}
	sort.Slice(out, func(i, j int) bool { return out[i][1].(int64) > out[j][1].(int64) })
	return trimCap(out)
}

func computeUnmapped(ctx context.Context, db *sql.DB, _ func() time.Time) ([][]any, bool, error) {
	// The unmapped predicate rides the idx_items_rpt_category ranges (NULL and
	// ''); the extension grouping then only touches unmapped rows.
	rows, err := db.QueryContext(ctx, `
		SELECT COALESCE(NULLIF(extension,''),'(none)'), COUNT(*), COALESCE(SUM(size),0)
		FROM items
		WHERE status='active' AND (file_category IS NULL OR file_category='')
		GROUP BY 1 ORDER BY 2 DESC LIMIT ?`, reportCap+1)
	if err != nil {
		return nil, false, err
	}
	defer rows.Close()
	return scanPlainRows(rows, 3)
}

func computeLargest(ctx context.Context, db *sql.DB, _ func() time.Time) ([][]any, bool, error) {
	// ORDER BY size DESC streams idx_items_rpt_size backwards — the cap keeps
	// row lookups (path/category) bounded.
	rows, err := db.QueryContext(ctx, `
		SELECT r.path || '/' || i.rel_path, COALESCE(i.file_category,''), i.size
		FROM items i JOIN roots r ON r.id = i.root_id
		WHERE i.status='active' AND i.is_sidecar=0
		ORDER BY i.size DESC LIMIT ?`, reportCap+1)
	if err != nil {
		return nil, false, err
	}
	defer rows.Close()
	return scanPlainRows(rows, 3)
}

func computeFutureDated(ctx context.Context, db *sql.DB, now func() time.Time) ([][]any, bool, error) {
	cutoff := now().UTC().UnixNano() + futureSlackNs
	rows, err := db.QueryContext(ctx, `
		SELECT r.path || '/' || i.rel_path,
			strftime('%Y-%m-%dT%H:%M:%SZ', i.mtime_ns/1000000000, 'unixepoch'), i.size
		FROM items i JOIN roots r ON r.id = i.root_id
		WHERE i.status='active' AND i.mtime_ns > ?
		ORDER BY i.mtime_ns DESC LIMIT ?`, cutoff, reportCap+1)
	if err != nil {
		return nil, false, err
	}
	defer rows.Close()
	return scanPlainRows(rows, 3)
}

// computeDuplicates runs the two hash tiers as SEPARATE grouped queries so
// each streams its own partial index (a single GROUP BY over a COALESCE
// expression can use neither), merges and ranks them in Go, caps, and only
// then resolves one sample path per SURVIVING group (bounded point lookups).
func computeDuplicates(ctx context.Context, db *sql.DB, _ func() time.Time) ([][]any, bool, error) {
	type group struct {
		tier   string
		hash   string
		size   int64 // quick-tier group key part; unused for content tier
		copies int64
		wasted int64
	}
	var groups []group

	collect := func(q, tier string) error {
		rows, err := db.QueryContext(ctx, q)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			g := group{tier: tier}
			if err := rows.Scan(&g.hash, &g.size, &g.copies, &g.wasted); err != nil {
				return err
			}
			groups = append(groups, g)
		}
		return rows.Err()
	}
	// Predicates textually imply the idx_items_rpt_chash / _qhash partial
	// index conditions — keep them in sync with index/schema.go.
	if err := collect(`
		SELECT content_hash, 0, COUNT(*), SUM(size)-MAX(size)
		FROM items
		WHERE status='active' AND is_sidecar=0 AND size>0
		  AND content_hash IS NOT NULL AND content_hash!=''
		GROUP BY content_hash HAVING COUNT(*)>1`, "content"); err != nil {
		return nil, false, err
	}
	if err := collect(`
		SELECT quick_hash, size, COUNT(*), SUM(size)-MAX(size)
		FROM items
		WHERE status='active' AND is_sidecar=0 AND size>0
		  AND (content_hash IS NULL OR content_hash='')
		  AND quick_hash IS NOT NULL AND quick_hash!=''
		GROUP BY quick_hash, size HAVING COUNT(*)>1`, "quick"); err != nil {
		return nil, false, err
	}

	sort.Slice(groups, func(i, j int) bool { return groups[i].wasted > groups[j].wasted })
	capped := len(groups) > reportCap
	if capped {
		groups = groups[:reportCap]
	}

	out := make([][]any, 0, len(groups))
	for _, g := range groups {
		var sample string
		var err error
		if g.tier == "content" {
			err = db.QueryRowContext(ctx, `
				SELECT r.path || '/' || i.rel_path FROM items i JOIN roots r ON r.id=i.root_id
				WHERE i.status='active' AND i.content_hash=? LIMIT 1`, g.hash).Scan(&sample)
		} else {
			err = db.QueryRowContext(ctx, `
				SELECT r.path || '/' || i.rel_path FROM items i JOIN roots r ON r.id=i.root_id
				WHERE i.status='active' AND i.quick_hash=? AND i.size=? LIMIT 1`, g.hash, g.size).Scan(&sample)
		}
		if err != nil && err != sql.ErrNoRows {
			return nil, false, err
		}
		out = append(out, []any{g.tier, g.copies, g.wasted, sample})
	}
	return out, capped, nil
}

func scanPlainRows(rows *sql.Rows, ncols int) ([][]any, bool, error) {
	var out [][]any
	for rows.Next() {
		vals := make([]any, ncols)
		ptrs := make([]any, ncols)
		for i := range vals {
			ptrs[i] = &vals[i]
		}
		if err := rows.Scan(ptrs...); err != nil {
			return nil, false, err
		}
		out = append(out, vals)
	}
	if err := rows.Err(); err != nil {
		return nil, false, err
	}
	return trimCap(out)
}

// trimCap converts a cap+1 overfetch into (capped rows, capped flag).
func trimCap(rows [][]any) ([][]any, bool, error) {
	if len(rows) > reportCap {
		return rows[:reportCap], true, nil
	}
	return rows, false, nil
}

// ReportSpecs returns the registry for GET /api/reports.
func ReportSpecs() []ReportSpec {
	out := make([]ReportSpec, len(reportDefs))
	for i, d := range reportDefs {
		out[i] = d.spec
	}
	return out
}

type reportCacheEntry struct {
	rows   [][]any
	capped bool
	at     time.Time
}

// NewReportsFn builds the cached report runner over the local index database.
// The handle may be the writable store's — every statement here is a SELECT.
// One compute runs per report id at a time; concurrent requests for the same
// id wait for the winner and share its cached result.
func NewReportsFn(db *sql.DB, now func() time.Time) func(ctx context.Context, id string, limit, offset int) (*ReportPage, error) {
	if now == nil {
		now = time.Now
	}
	var mu sync.Mutex
	cache := map[string]*reportCacheEntry{}
	inflight := map[string]chan struct{}{}

	return func(ctx context.Context, id string, limit, offset int) (*ReportPage, error) {
		var def *reportDef
		for i := range reportDefs {
			if reportDefs[i].spec.ID == id {
				def = &reportDefs[i]
				break
			}
		}
		if def == nil {
			return nil, nil // unknown id -> 404 at the handler
		}

		for {
			mu.Lock()
			if e, ok := cache[id]; ok && now().Sub(e.at) < reportCacheTTL {
				mu.Unlock()
				return pageOf(def.spec, e, limit, offset), nil
			}
			if ch, busy := inflight[id]; busy {
				mu.Unlock()
				select { // wait for the concurrent compute, then re-check cache
				case <-ch:
					continue
				case <-ctx.Done():
					return nil, ctx.Err()
				}
			}
			ch := make(chan struct{})
			inflight[id] = ch
			mu.Unlock()

			// Detached context: the compute keeps its own budget so an
			// impatient browser abort doesn't waste the work for the next hit.
			cctx, cancel := context.WithTimeout(context.Background(), reportTimeout)
			rows, capped, err := def.compute(cctx, db, now)
			cancel()

			mu.Lock()
			delete(inflight, id)
			close(ch)
			if err != nil {
				mu.Unlock()
				return nil, err
			}
			e := &reportCacheEntry{rows: rows, capped: capped, at: now()}
			cache[id] = e
			mu.Unlock()
			return pageOf(def.spec, e, limit, offset), nil
		}
	}
}

func pageOf(spec ReportSpec, e *reportCacheEntry, limit, offset int) *ReportPage {
	n := len(e.rows)
	lo := offset
	if lo > n {
		lo = n
	}
	hi := lo + limit
	if hi > n {
		hi = n
	}
	return &ReportPage{
		Spec: spec, Rows: e.rows[lo:hi], Total: n,
		Limit: limit, Offset: offset, Capped: e.capped,
		ComputedAt: e.at.UTC().Format(time.RFC3339),
	}
}
