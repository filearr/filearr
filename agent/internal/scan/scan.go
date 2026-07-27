package scan

import (
	"context"
	"database/sql"
	"errors"
	"path"
	"path/filepath"
	"time"

	"github.com/filearr/filearr/agent/internal/index"
	"github.com/filearr/filearr/agent/internal/outbox"
)

// flushEvery is the batch-commit cadence: commit + publish progress + check for
// cancellation every 250 files. Mirrors scan.py:FLUSH_EVERY.
const flushEvery = 250

// Progress is published at each batch boundary during a scan.
type Progress struct {
	Seen    int
	New     int
	Changed int
}

// Options configures one Scan. Root is the absolute filesystem root; StartRel
// (optional) confines the walk to a subtree while item identity stays relative
// to Root. When Spec is nil it is built from EnabledPresets/ExcludeGlobs/
// IncludeGlobs. EnabledCategories/EnabledGroups gate the File Extension
// Similarity Taxonomy at walk time, mirroring central's library model: a file is
// included iff BOTH are empty (all files) OR its file_category is in
// EnabledCategories OR its file_group is in EnabledGroups (sidecars always bypass
// the gate). Taxonomy classifies each file into (file_category, file_group);
// when nil the baked-in seed taxonomy is used (W8-E). ShouldStop, when it returns
// true between batches, requests a graceful stop (skips move detection AND
// tombstoning).
type Options struct {
	Root              string
	StartRel          string
	EnabledPresets    []string
	ExcludeGlobs      []string
	IncludeGlobs      []string
	Spec              *Spec
	EnabledCategories []string
	EnabledGroups     []string
	Taxonomy          Classifier
	Hash              HashPolicy
	Progress          func(Progress)
	ShouldStop        func() bool
	// Shares (optional, P10-T11) discovers the network-share hint for each
	// created/modified file. Nil = no discovery (hints omitted). Best-effort: a
	// nil Hint for an uncovered path is normal, never an error.
	Shares ShareResolver
}

// Result summarises a completed scan. Stopped marks a graceful stop (Missing and
// Moved are 0 by construction); ScopeMissing marks a scoped run whose subtree
// does not exist (nothing written).
type Result struct {
	Root          string
	RootID        string
	Seen          int
	New           int
	Changed       int
	Missing       int
	Moved         int
	MoveAmbiguous int
	Sidecars      SidecarStats
	Stopped       bool
	ScopeMissing  bool
}

// errGracefulStop unwinds the walk on a between-batch stop request.
var errGracefulStop = errors.New("scan: graceful stop requested")

// Scan performs one library scan against the local index: pre-flight guard →
// walk → diff (batched commits) → move detection → tombstone → sidecar
// association. It is a behavioural port of scan._scan_body. A dead/unreadable
// root fails the scan (returns *ScanRootError) rather than tombstoning
// everything (invariant 7). Cancelling ctx hard-aborts (committed batches
// persist, no move/tombstone); ShouldStop requests a clean graceful stop.
func Scan(ctx context.Context, store *index.Store, opts Options) (Result, error) {
	root, err := filepath.Abs(opts.Root)
	if err != nil {
		return Result{}, err
	}
	if err := assertScannableRoot(root); err != nil {
		return Result{}, err
	}
	scope := normScope(opts.StartRel)
	if scopeDirMissing(root, scope) {
		// A pre-created hot folder that does not exist yet must not tombstone the
		// items recorded under it. Finish clean, writing nothing.
		return Result{Root: root, ScopeMissing: true}, nil
	}

	spec := opts.Spec
	if spec == nil {
		spec = BuildLibrarySpec(opts.EnabledPresets, opts.ExcludeGlobs, opts.IncludeGlobs)
	}
	policy := opts.Hash
	if policy.FullMaxBytes == 0 && !policy.ComputeContent {
		policy = DefaultHashPolicy()
	}

	rootID, err := ensureRootID(ctx, store, root)
	if err != nil {
		return Result{}, err
	}
	existing, err := store.LoadItems(ctx, rootID)
	if err != nil {
		return Result{}, err
	}

	classifier := opts.Taxonomy
	if classifier == nil {
		classifier = seedClassifier()
	}
	enabledCats := map[string]bool{}
	for _, c := range opts.EnabledCategories {
		enabledCats[c] = true
	}
	enabledGroups := map[string]bool{}
	for _, g := range opts.EnabledGroups {
		enabledGroups[g] = true
	}

	res := Result{Root: root, RootID: rootID}
	seen := map[string]bool{}
	var newItems []*index.Item

	// SQLITE_BUSY fix (live Unraid incident 2026-07-27): the batch write
	// transaction used to be OPENED FIRST and held while every entry in the
	// batch was hashed. On a local disk that window is milliseconds; over a
	// FUSE mount (Unraid /mnt/user shfs) hashing a batch of new media takes
	// minutes — and the `run` daemon's replicator, sharing the same SQLite
	// file from its own process, hit its busy timeout and backed off 32s at a
	// time. Now every per-entry DECISION (classify, diff against the
	// preloaded map, hash, share-hint resolution — all the slow I/O) runs
	// with NO transaction, accumulating apply-closures; the batch boundary
	// opens one short transaction that only executes buffered INSERT/UPDATE/
	// outbox writes. The write lock is now held for pure-DB work only.
	var pending []applyFn
	flush := func() error {
		if len(pending) == 0 {
			return nil
		}
		tx, err := store.Begin(ctx)
		if err != nil {
			return err
		}
		for _, apply := range pending {
			if err := apply(ctx, tx); err != nil {
				_ = tx.Rollback()
				return err
			}
		}
		if err := tx.Commit(); err != nil {
			return err
		}
		pending = pending[:0]
		return nil
	}

	stopRequested := false
	walkErr := Walk(root, scope, spec, func(e WalkEntry) error {
		if err := ctx.Err(); err != nil {
			return err // hard cancel
		}
		sidecar := isSidecar(e.Rel)
		category, group := classifier.Classify(e.Path)
		if !sidecar && !categoryEnabled(enabledCats, enabledGroups, category, group) {
			return nil // file_category/file_group excluded for this library
		}
		seen[e.Rel] = true
		apply := diffEntry(root, rootID, existing, e, category, group, sidecar, policy, &res, &newItems, opts.Shares)
		if apply != nil {
			pending = append(pending, apply)
		}
		if len(seen)%flushEvery == 0 {
			if err := flush(); err != nil {
				return err
			}
			if opts.Progress != nil {
				opts.Progress(Progress{Seen: len(seen), New: res.New, Changed: res.Changed})
			}
			if opts.ShouldStop != nil && opts.ShouldStop() {
				return errGracefulStop
			}
			if err := ctx.Err(); err != nil {
				return err
			}
		}
		return nil
	})
	// Persist the final partial batch before interpreting the walk outcome.
	if cErr := flush(); cErr != nil {
		return Result{}, cErr
	}
	switch {
	case errors.Is(walkErr, errGracefulStop):
		stopRequested = true
	case walkErr != nil:
		// A hard cancel or a genuine walk error: committed batches persist, but no
		// move detection or tombstoning runs (the visited set is unreliable).
		return res, walkErr
	}

	if !stopRequested {
		if err := detectAndTombstone(ctx, store, root, rootID, existing, seen, scope, newItems, &res); err != nil {
			return Result{}, err
		}
	}

	// Sidecar association is idempotent and derives from the full active row set,
	// so it is safe after a partial (graceful-stop) walk too.
	sc, err := associateSidecars(ctx, store, rootID, classifier)
	if err != nil {
		return Result{}, err
	}
	res.Sidecars = sc
	res.Stopped = stopRequested
	res.Seen = len(seen)
	return res, nil
}

// applyFn is one buffered batch write: everything slow (hashing, share-hint
// resolution, classification) already happened tx-free at decision time; the
// closure only executes the prepared INSERT/UPDATE + outbox emit.
type applyFn func(context.Context, *sql.Tx) error

// diffEntry classifies one walked file against the in-memory existing map and
// returns the batch write to apply (nil = steady-state no-op). All decisions,
// hashing, and share-hint resolution happen HERE, with no transaction open
// (see the flush() note in Scan — holding the write lock across FUSE hashing
// starved the daemon's replicator into SQLITE_BUSY backoffs). Mirrors the
// per-file body of scan._scan_body. Local deviation (documented): an
// unchanged, healthy row (active + already hashed) is a NO-OP — the agent
// does not rewrite last_seen on every steady-state file, since that would
// churn local_seq_no and flood the P5-T4 replication delta; the in-memory
// `seen` set (not last_seen) drives tombstoning here.
func diffEntry(
	libraryRef, rootID string, existing map[string]*index.Item,
	e WalkEntry, category, group string, sidecar bool, policy HashPolicy,
	res *Result, newItems *[]*index.Item, sh ShareResolver,
) applyFn {
	now := time.Now().UTC()
	item := existing[e.Rel]
	if item == nil {
		id, err := index.NewID()
		if err != nil {
			return func(context.Context, *sql.Tx) error { return err }
		}
		it := &index.Item{
			ID:           id,
			RootID:       rootID,
			RelPath:      e.Rel,
			Filename:     path.Base(e.Rel),
			Extension:    fileExtension(path.Base(e.Rel)),
			Size:         e.Size,
			MtimeNs:      e.MtimeNs,
			FileCategory: category,
			FileGroup:    group,
			Status:       index.StatusActive,
			IsSidecar:    sidecar,
			FirstSeen:    now,
			LastSeen:     now,
		}
		if !sidecar {
			it.QuickHash, it.ContentHash = hashFile(e.Path, e.Size, policy)
		}
		hint := shareHint(sh, e.Path)
		existing[e.Rel] = it
		res.New++
		if !sidecar {
			*newItems = append(*newItems, it)
		}
		return func(ctx context.Context, tx *sql.Tx) error {
			if err := index.InsertItem(ctx, tx, it); err != nil {
				return err
			}
			// A brand-new file → created (central collapses created/modified to
			// an upsert). Sidecars are emitted too: plain items on the wire.
			return emit(ctx, tx, libraryRef, outbox.OpCreated, it, "", hint)
		}
	}
	if item.Size != e.Size || item.MtimeNs != e.MtimeNs {
		item.Size = e.Size
		item.MtimeNs = e.MtimeNs
		item.FileCategory = category
		item.FileGroup = group
		item.Status = index.StatusActive
		item.LastSeen = now
		if !sidecar {
			item.QuickHash, item.ContentHash = hashFile(e.Path, e.Size, policy)
		}
		hint := shareHint(sh, e.Path)
		res.Changed++
		return func(ctx context.Context, tx *sql.Tx) error {
			if err := index.UpdateItem(ctx, tx, item); err != nil {
				return err
			}
			return emit(ctx, tx, libraryRef, outbox.OpModified, item, "", hint)
		}
	}
	// Unchanged: self-heal only. A missing row reappearing goes back to active; a
	// committed-but-never-hashed row is hashed now (null-quick_hash self-heal).
	needHeal := false
	if item.Status == index.StatusMissing {
		item.Status = index.StatusActive
		needHeal = true
	}
	if item.QuickHash == "" && !sidecar {
		item.QuickHash, item.ContentHash = hashFile(e.Path, e.Size, policy)
		needHeal = true
	}
	if !needHeal {
		return nil // truly unchanged healthy row: no write, no emit
	}
	item.LastSeen = now
	hint := shareHint(sh, e.Path)
	return func(ctx context.Context, tx *sql.Tx) error {
		if err := index.UpdateItem(ctx, tx, item); err != nil {
			return err
		}
		// Self-heal (missing→active, or a late hash fill) is a real change
		// central must see → modified. A healthy row emits nothing (above).
		return emit(ctx, tx, libraryRef, outbox.OpModified, item, "", hint)
	}
}

// detectAndTombstone runs move detection (before tombstoning) then tombstones
// unseen active rows. Both are confined to the scope. Mirrors the move/tombstone
// tail of scan._scan_body.
func detectAndTombstone(
	ctx context.Context, store *index.Store, libraryRef, rootID string,
	existing map[string]*index.Item, seen map[string]bool, scope string,
	newItems []*index.Item, res *Result,
) error {
	tx, err := store.Begin(ctx)
	if err != nil {
		return err
	}
	defer func() {
		if tx != nil {
			_ = tx.Rollback()
		}
	}()

	// Candidates: prior-scan rows that vanished from their rel_path AND carry a
	// quick_hash (sidecars have none, so they are excluded).
	var candidates []*index.Item
	for rel, item := range existing {
		if underScope(rel, scope) && !seen[rel] && item.Status == index.StatusActive && item.QuickHash != "" {
			candidates = append(candidates, item)
		}
	}
	moved, ambiguous, _, err := detectMoves(ctx, tx, libraryRef, candidates, newItems)
	if err != nil {
		return err
	}
	res.Moved = moved
	res.MoveAmbiguous = ambiguous
	// `moved` rows were counted as New during the walk (freshly inserted before
	// being recognised as relocations); reclassify like scan.py.
	res.New -= moved

	// Tombstone unseen active rows. A candidate whose identity was transferred was
	// repointed onto a seen rel_path, so item.RelPath ∈ seen and it is skipped.
	missing := 0
	for rel, item := range existing {
		if underScope(rel, scope) && !seen[rel] && !seen[item.RelPath] && item.Status == index.StatusActive {
			item.Status = index.StatusMissing
			if err := index.UpdateItem(ctx, tx, item); err != nil {
				return err
			}
			// Tombstone → deleted. Central tombstones (library_ref, rel_path); a
			// late delete against an already-purged central row is an idempotent
			// no-op there (agentsync R2).
			if err := emit(ctx, tx, libraryRef, outbox.OpDeleted, item, "", nil); err != nil {
				return err
			}
			missing++
		}
	}
	res.Missing = missing

	c := tx
	tx = nil
	return c.Commit()
}

// ensureRootID resolves (or creates) the roots row id for absPath in its own tx.
func ensureRootID(ctx context.Context, store *index.Store, absPath string) (string, error) {
	tx, err := store.Begin(ctx)
	if err != nil {
		return "", err
	}
	id, err := index.EnsureRoot(ctx, tx, absPath)
	if err != nil {
		_ = tx.Rollback()
		return "", err
	}
	if err := tx.Commit(); err != nil {
		return "", err
	}
	return id, nil
}
