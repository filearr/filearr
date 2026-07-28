package localapi

import (
	"context"
	"database/sql"
	"time"
)

// Local reporting (user request 2026-07-27): the agent-side counterpart of the
// console's canned Reports page, computed over the LOCAL index only. The report
// set mirrors central's where the local schema can answer (unmapped extensions,
// future-dated files, largest files, duplicates) plus a categories rollup that
// also feeds the search tab's category chips. Every query is a code-controlled
// SELECT with bound parameters — the id is looked up in the registry, never
// interpolated.

// ReportSpec describes one canned report for the list endpoint.
type ReportSpec struct {
	ID          string   `json:"id"`
	Title       string   `json:"title"`
	Description string   `json:"description"`
	Columns     []string `json:"columns"`
	// ByteCols marks columns the UI right-aligns and renders human-sized.
	ByteCols []string `json:"byte_cols,omitempty"`
}

// ReportPage is one page of report rows plus the exact total.
type ReportPage struct {
	Spec   ReportSpec `json:"spec"`
	Rows   [][]any    `json:"rows"`
	Total  int        `json:"total"`
	Limit  int        `json:"limit"`
	Offset int        `json:"offset"`
}

type reportDef struct {
	spec  ReportSpec
	query string // SELECT producing exactly spec.Columns, ending with LIMIT ? OFFSET ?
	count string // SELECT COUNT for the same predicate
}

// futureSlackNs mirrors central's bad_mtime report: flag mtimes >48h ahead.
const futureSlackNs = int64(48 * time.Hour)

var reportDefs = []reportDef{
	{
		spec: ReportSpec{
			ID: "categories", Title: "Categories",
			Description: "Active items and bytes per taxonomy category.",
			Columns:     []string{"category", "files", "total_bytes"},
			ByteCols:    []string{"total_bytes"},
		},
		query: `SELECT COALESCE(NULLIF(file_category,''),'(unclassified)'), COUNT(*), COALESCE(SUM(size),0)
			FROM items WHERE status='active'
			GROUP BY 1 ORDER BY 2 DESC LIMIT ? OFFSET ?`,
		count: `SELECT COUNT(DISTINCT COALESCE(NULLIF(file_category,''),'(unclassified)'))
			FROM items WHERE status='active'`,
	},
	{
		spec: ReportSpec{
			ID: "unmapped_extensions", Title: "Unmapped extensions",
			Description: "Extensions the taxonomy does not classify, by file count.",
			Columns:     []string{"extension", "files", "total_bytes"},
			ByteCols:    []string{"total_bytes"},
		},
		query: `SELECT COALESCE(NULLIF(extension,''),'(none)'), COUNT(*), COALESCE(SUM(size),0)
			FROM items WHERE status='active' AND (file_category IS NULL OR file_category='')
			GROUP BY 1 ORDER BY 2 DESC LIMIT ? OFFSET ?`,
		count: `SELECT COUNT(DISTINCT COALESCE(NULLIF(extension,''),'(none)'))
			FROM items WHERE status='active' AND (file_category IS NULL OR file_category='')`,
	},
	{
		spec: ReportSpec{
			ID: "largest_files", Title: "Largest files",
			Description: "Active items by size, descending.",
			Columns:     []string{"path", "category", "size"},
			ByteCols:    []string{"size"},
		},
		query: `SELECT r.path || '/' || i.rel_path, COALESCE(i.file_category,''), i.size
			FROM items i JOIN roots r ON r.id = i.root_id
			WHERE i.status='active' AND i.is_sidecar=0
			ORDER BY i.size DESC LIMIT ? OFFSET ?`,
		count: `SELECT COUNT(*) FROM items WHERE status='active' AND is_sidecar=0`,
	},
	{
		spec: ReportSpec{
			ID: "duplicate_files", Title: "Duplicate files",
			Description: "Hash groups with more than one copy (content hash when known, else sampled quick hash + size — a strong but not byte-certain signal).",
			Columns:     []string{"tier", "copies", "wasted_bytes", "sample_path"},
			ByteCols:    []string{"wasted_bytes"},
		},
		query: `SELECT CASE WHEN i.content_hash IS NOT NULL AND i.content_hash!='' THEN 'content' ELSE 'quick' END,
			COUNT(*), COALESCE(SUM(i.size),0)-COALESCE(MAX(i.size),0),
			MIN(r.path || '/' || i.rel_path)
			FROM items i JOIN roots r ON r.id = i.root_id
			WHERE i.status='active' AND i.is_sidecar=0 AND i.size>0
			  AND ((i.content_hash IS NOT NULL AND i.content_hash!='') OR (i.quick_hash IS NOT NULL AND i.quick_hash!=''))
			GROUP BY COALESCE(NULLIF(i.content_hash,''), i.quick_hash || ':' || i.size)
			HAVING COUNT(*) > 1
			ORDER BY 3 DESC LIMIT ? OFFSET ?`,
		count: `SELECT COUNT(*) FROM (
			SELECT 1 FROM items
			WHERE status='active' AND is_sidecar=0 AND size>0
			  AND ((content_hash IS NOT NULL AND content_hash!='') OR (quick_hash IS NOT NULL AND quick_hash!=''))
			GROUP BY COALESCE(NULLIF(content_hash,''), quick_hash || ':' || size)
			HAVING COUNT(*) > 1)`,
	},
	{
		spec: ReportSpec{
			ID: "future_dated", Title: "Future-dated files",
			Description: "Items whose modification time is more than 48 hours in the future (clock or filesystem damage).",
			Columns:     []string{"path", "mtime", "size"},
			ByteCols:    []string{"size"},
		},
		query: `SELECT r.path || '/' || i.rel_path,
			strftime('%Y-%m-%dT%H:%M:%SZ', i.mtime_ns/1000000000, 'unixepoch'), i.size
			FROM items i JOIN roots r ON r.id = i.root_id
			WHERE i.status='active' AND i.mtime_ns > ?
			ORDER BY i.mtime_ns DESC LIMIT ? OFFSET ?`,
		count: `SELECT COUNT(*) FROM items WHERE status='active' AND mtime_ns > ?`,
	},
}

// ReportSpecs returns the registry for GET /api/reports.
func ReportSpecs() []ReportSpec {
	out := make([]ReportSpec, len(reportDefs))
	for i, d := range reportDefs {
		out[i] = d.spec
	}
	return out
}

// NewReportsFn builds the report runner over the local index database. The
// handle may be the writable store's — every statement here is a SELECT.
func NewReportsFn(db *sql.DB, now func() time.Time) func(ctx context.Context, id string, limit, offset int) (*ReportPage, error) {
	if now == nil {
		now = time.Now
	}
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
		args := []any{}
		cargs := []any{}
		if def.spec.ID == "future_dated" {
			cutoff := now().UTC().UnixNano() + futureSlackNs
			args = append(args, cutoff)
			cargs = append(cargs, cutoff)
		}
		args = append(args, limit, offset)

		page := &ReportPage{Spec: def.spec, Rows: [][]any{}, Limit: limit, Offset: offset}
		if err := db.QueryRowContext(ctx, def.count, cargs...).Scan(&page.Total); err != nil {
			return nil, err
		}
		rows, err := db.QueryContext(ctx, def.query, args...)
		if err != nil {
			return nil, err
		}
		defer rows.Close()
		ncols := len(def.spec.Columns)
		for rows.Next() {
			vals := make([]any, ncols)
			ptrs := make([]any, ncols)
			for i := range vals {
				ptrs[i] = &vals[i]
			}
			if err := rows.Scan(ptrs...); err != nil {
				return nil, err
			}
			page.Rows = append(page.Rows, vals)
		}
		return page, rows.Err()
	}
}
