// Package reextract is the agent's operator-triggered re-extraction sweep
// (agent parity phase 3).
//
// # Why it exists
//
// The extraction pass runs INSIDE the scan, over the files that scan reports as
// new or changed (scan.diffEntry — the unchanged-but-healthy branch deliberately
// emits no extraction, because the bytes have not moved). That is right for
// steady state and wrong for exactly one situation: an item catalogued BEFORE
// extract_enabled was turned on, or before its host gained ffprobe/exiftool/
// poppler/tesseract, carries identity-only metadata forever — no future scan will
// ever look at it again. This package sweeps the EXISTING local index, re-runs
// extraction over items whose bytes are demonstrably unchanged, and re-emits them
// as ordinary `modified` events so central applies the result into Item.metadata_
// (invariant 2) exactly as it does for a scan-produced one.
//
// # What it is NOT
//
// It is not a scan. It never walks the filesystem, never creates, tombstones or
// rehashes anything, and never touches the items table. A candidate whose size or
// mtime disagrees with the index row is SKIPPED, not repaired: a changed file is
// the scan's job, and emitting the index's stale identity for it would push wrong
// size/hashes to central. The sweep's only writes are outbox events and its own
// cursor.
package reextract

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/filearr/filearr/agent/internal/index"
	"github.com/filearr/filearr/agent/internal/outbox"
)

const (
	// DefaultMaxItems bounds how many candidates ONE command examines. The bound
	// is on items SEEN, not extracted, so it has to survive both shapes of sweep:
	// a library of mostly unsupported categories races through candidates doing
	// almost no work (a small cap would then need dozens of commands to cover
	// nothing), while a video library pays an ffprobe subprocess per item. 250k
	// splits the largest deployed agent (~1.09M items, live 2026-07-18) into four
	// or five resumable chunks — each one an operator can cancel, and each one
	// bounded enough that a mistake costs minutes, not a night.
	DefaultMaxItems = 250_000

	// DefaultBatchSize mirrors scan.flushEvery (250): the same commit cadence,
	// chosen for the same reason — it is the granularity at which a crash loses
	// work, and the granularity at which the write lock is taken and released.
	DefaultBatchSize = 250

	// commitTimeout bounds the detached batch commit (see the commit phase in
	// Run). Purely a deadlock guard: the write is pure-DB work against a store
	// whose driver already carries a 30s busy timeout.
	commitTimeout = 2 * time.Minute
)

// Stop reasons, reported verbatim in Result.Reason and posted back to central as
// part of the command result — an operator reads these, so they are sentences,
// not error codes.
const (
	reasonAlreadyDone = "already re-extracted at this configuration"
	reasonCancelled   = "cancelled"
	reasonPaused      = "agent suspended or central in maintenance"
	reasonMaxItems    = "max_items reached"
)

// ExtractFn is the extraction seam, structurally identical to scan.ExtractFn and
// declared here rather than imported so this package stays free of the scan
// dependency (the same posture commands.Config.TriggerUpdate takes towards the
// update package). The daemon's scan.ExtractFn converts to it directly.
//
// Contract, inherited verbatim from the scan seam: best-effort, self-contained,
// and nil for "extraction produced nothing" — an internal failure is swallowed by
// the seam, never propagated, because a file the agent cannot parse must not fail
// the pass around it.
type ExtractFn func(ctx context.Context, absPath, category string) *outbox.Extracted

// Options configures one sweep.
type Options struct {
	Force     bool        // ignore the "already done at this configuration" short-circuit
	MaxItems  int         // stop after this many candidates SEEN (<=0 => DefaultMaxItems)
	FP        string      // extraction-configuration fingerprint (see Fingerprint)
	Extract   ExtractFn   // the extraction seam; nil => the sweep refuses to run
	Paused    func() bool // suspend / central-maintenance gate; nil => never paused
	BatchSize int         // items per transaction (<=0 => DefaultBatchSize)
	Log       *slog.Logger
	Now       func() time.Time
}

// Result reports what ONE run did. The counters are this run's, not the sweep's:
// MaxItems bounds a single command, so a chunked sweep reports each chunk here
// while the persisted index.ExtractState accumulates the totals across chunks.
type Result struct {
	Seen, Extracted, Skipped, Failed int64

	Completed bool   // the index was walked to the end
	Resumed   bool   // this run continued a previous incomplete sweep
	Cursor    int64  // durable rowid frontier after this run
	Reason    string // why it stopped, when it did not complete
}

// Fingerprint renders the extraction CONFIGURATION as a short stable string.
// Anything that changes what an extraction would PRODUCE belongs in it: the
// schema version, the policy knobs, and which host tools resolved. Two runs with
// equal fingerprints would produce equal results, which is precisely what makes
// the idempotence short-circuit safe; a differing fingerprint invalidates the
// cursor and re-sweeps from the beginning.
//
// tools is keyed by tool name; only the ones that RESOLVED are folded in, sorted,
// so the value depends on what the host can actually do and not on the order Go
// happened to iterate the map (or on a build that reports a longer vocabulary of
// absent tools). The human-readable prefix is the extraction schema version,
// because that is the one component an operator reading the local status page can
// act on: a schema bump means every agent re-extracts, by design.
func Fingerprint(schema int, bodyText, ocr, exif bool, maxBytes int64, tools map[string]bool) string {
	names := make([]string, 0, len(tools))
	for name, ok := range tools {
		if ok {
			names = append(names, name)
		}
	}
	sort.Strings(names)
	canonical := strings.Join([]string{
		"schema=" + strconv.Itoa(schema),
		"body=" + boolDigit(bodyText),
		"ocr=" + boolDigit(ocr),
		"exif=" + boolDigit(exif),
		"max=" + strconv.FormatInt(maxBytes, 10),
		"tools=" + strings.Join(names, ","),
	}, ";")
	sum := sha256.Sum256([]byte(canonical))
	// 12 hex chars (48 bits) is plenty: this is a change DETECTOR over a handful
	// of local configurations, not a security boundary, and a short value is one
	// an operator can compare by eye on the status page.
	return "e" + strconv.Itoa(schema) + "-" + hex.EncodeToString(sum[:])[:12]
}

func boolDigit(b bool) string {
	if b {
		return "1"
	}
	return "0"
}

// Run performs one sweep (or one chunk of one). It returns an error only for a
// framing failure — a store read/write it cannot recover from, or a missing
// extraction seam; everything a single file can do to it is a skip.
func Run(ctx context.Context, st *index.Store, opts Options) (Result, error) {
	opts = opts.withDefaults()
	if opts.Extract == nil {
		// A sweep with no extractor would walk the whole index, skip every item,
		// and then stamp the configuration as "done" — poisoning the idempotence
		// short-circuit for the configuration the operator will actually enable
		// next. Refusing is the honest answer, and it reaches the operator as the
		// command's error.
		return Result{}, fmt.Errorf("reextract: extraction is not enabled on this agent (no extraction seam)")
	}

	stored, err := st.ExtractState(ctx)
	if err != nil {
		return Result{}, err
	}

	var (
		res   Result
		state index.ExtractState
	)
	switch {
	case !opts.Force && stored.FP == opts.FP && stored.FinishedAt != "":
		// Idempotence: this exact configuration has already been swept to the end.
		// A repeat command (a retry, a double click, a fleet-wide broadcast) must
		// not re-read a million files to produce results central already has.
		return Result{Completed: true, Cursor: stored.CursorRowID, Reason: reasonAlreadyDone}, nil
	case !opts.Force && stored.FP == opts.FP:
		// Resume: same configuration, unfinished sweep. This is what makes a
		// MaxItems-bounded run chunkable — the operator simply sends the command
		// again and it picks up at the durable cursor.
		state = stored
		res.Resumed = true
	default:
		// Invalidate: the configuration changed (a policy knob, or a host tool
		// appeared) or the operator forced it, so everything already swept was
		// swept under rules that no longer apply. Start over from rowid 0.
		state = index.ExtractState{FP: opts.FP, StartedAt: opts.Now().UTC().Format(time.RFC3339Nano)}
	}
	// Persist the frontier before any work, so a run that is paused or cancelled
	// before its first batch still leaves the fingerprint and start time behind
	// for the next attempt to resume from.
	if err := st.SaveExtractState(ctx, state); err != nil {
		return res, err
	}

	maxItems := int64(opts.MaxItems)
	opts.Log.Info("re-extraction sweep starting",
		"fp", opts.FP, "resumed", res.Resumed, "cursor", state.CursorRowID, "max_items", maxItems)
	started := opts.Now()

	stopReason := ""
	completed := false
	for {
		// Stop conditions are evaluated once per BATCH, not per item: Paused may
		// hit the daemon's state and MaxItems is a coarse bound, so paying for
		// either 250 times per commit would be waste. Cancellation is additionally
		// checked per item below, where an abandoned extraction is the thing worth
		// aborting.
		switch {
		case ctx.Err() != nil:
			stopReason = reasonCancelled
		case opts.Paused != nil && opts.Paused():
			stopReason = reasonPaused
		case res.Seen >= maxItems:
			stopReason = reasonMaxItems
		}
		if stopReason != "" {
			break // the previous iteration already committed its cursor
		}

		limit := opts.BatchSize
		if remaining := maxItems - res.Seen; remaining < int64(limit) {
			limit = int(remaining)
		}
		cands, err := st.ExtractCandidates(ctx, state.CursorRowID, limit)
		if err != nil {
			return res, err
		}
		// A short read means the index has no rows past the cursor: the sweep is
		// complete. Deciding it HERE (rather than on a following empty query) lets
		// the FinishedAt stamp ride the same transaction as the final batch's
		// events.
		if len(cands) < limit {
			completed = true
		}

		// --- extraction phase: NO transaction open ---------------------------
		//
		// Same discipline as scan.Scan's flush(): every slow thing (stat, and an
		// extraction that may spawn ffprobe/tesseract over a FUSE mount) happens
		// with the write lock released, and only the prepared writes are executed
		// inside the batch transaction. Holding SQLite's single writer across a
		// batch of extractions is precisely what starved the daemon's replicator
		// into SQLITE_BUSY backoffs during scans (live Unraid incident 2026-07-27).
		var (
			events []outbox.Event
			batch  index.ExtractState // per-batch counter deltas
			cursor = state.CursorRowID
		)
		for _, c := range cands {
			if ctx.Err() != nil {
				stopReason = reasonCancelled
				break // the cursor stays behind this item: it is re-examined next run
			}
			cursor = c.RowID
			batch.Seen++

			abs := filepath.Join(c.RootPath, filepath.FromSlash(c.RelPath))
			info, statErr := os.Stat(abs)
			if statErr != nil || !info.Mode().IsRegular() {
				// Vanished, unreadable, or no longer a regular file. Not this
				// pass's problem: the next scan tombstones it (invariant 4), and a
				// re-extract must never make that decision itself.
				batch.Skipped++
				continue
			}
			if info.Size() != c.Size || info.ModTime().UnixNano() != c.MtimeNs {
				// The bytes moved since the index last saw them, so the row's
				// identity (size, mtime, both hashes) is stale. Re-emitting it with
				// a fresh extraction would push WRONG identity to central under the
				// guise of an enrichment. The scan owns changed files.
				batch.Skipped++
				continue
			}
			ext := opts.Extract(ctx, abs, c.FileCategory)
			if ext == nil {
				// The normal outcome for most of an index: extraction is
				// unsupported for the category, the file exceeds the size cap, or
				// there was simply nothing to find.
				batch.Skipped++
				continue
			}
			events = append(events, outbox.Event{
				ItemID:     c.ID,
				Op:         outbox.OpModified,
				LibraryRef: c.RootPath,
				RelPath:    c.RelPath,
				// Identity verbatim from the index row: the file is unchanged (the
				// stat above proved it), so these are still true and re-deriving
				// them would be work with no answer of its own.
				Size:        c.Size,
				MtimeNs:     c.MtimeNs,
				QuickHash:   c.QuickHash,
				ContentHash: c.ContentHash,
				// No ShareHint: this package has no share resolver, and central
				// treats an absent hint on a modified event as "no update", leaving
				// a previously-good hint intact (agentsync.py apply path). The scan
				// owns hints.
				Extracted: ext,
			})
			batch.Extracted++
		}

		// A batch abandoned mid-flight has NOT reached the end of the index, even
		// if the short-read check above already guessed it would: the items past
		// the break were never examined, so the completion stamp must not ride
		// this commit.
		if stopReason != "" {
			completed = false
		}

		// --- commit phase: events + cursor, one short transaction -------------
		if err := commitBatch(ctx, st, events, &state, cursor, batch, completed, opts.Now); err != nil {
			return res, err
		}
		res.Seen += batch.Seen
		res.Extracted += batch.Extracted
		res.Skipped += batch.Skipped
		res.Failed += batch.Failed

		if completed || stopReason != "" {
			break
		}
	}

	res.Cursor = state.CursorRowID
	res.Completed = completed
	if !completed {
		res.Reason = stopReason
	}
	opts.Log.Info("re-extraction sweep finished",
		"seen", res.Seen, "extracted", res.Extracted, "skipped", res.Skipped,
		"completed", res.Completed, "reason", res.Reason, "cursor", res.Cursor,
		"duration", opts.Now().Sub(started).Round(time.Second).String())
	return res, nil
}

// commitBatch writes one batch's events and the advanced cursor in a SINGLE
// transaction, so a crash resumes exactly where the last durable event landed:
// either both the events and the cursor that covers them are visible, or neither
// is (index.SaveExtractStateTx exists for this).
//
// The transaction runs on a ctx detached from cancellation (bounded by
// commitTimeout) for the same reason outbox.MarkSent does: the expensive part —
// the extraction itself — is already paid for, and losing a whole batch of it to
// a shutdown racing the commit would mean re-reading those files on the next run.
// The loop above is what honours cancellation; this write deliberately finishes.
func commitBatch(
	ctx context.Context, st *index.Store, events []outbox.Event,
	state *index.ExtractState, cursor int64, batch index.ExtractState,
	completed bool, now func() time.Time,
) error {
	next := *state
	// cursor is the rowid of the last candidate the loop fully EXAMINED, so a run
	// that broke mid-batch leaves the frontier behind the item it abandoned and
	// that item is re-examined next run. It never moves backwards.
	if cursor > next.CursorRowID {
		next.CursorRowID = cursor
	}
	next.Seen += batch.Seen
	next.Extracted += batch.Extracted
	next.Skipped += batch.Skipped
	next.Failed += batch.Failed
	if completed {
		next.FinishedAt = now().UTC().Format(time.RFC3339Nano)
	}

	writeCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), commitTimeout)
	defer cancel()

	tx, err := st.Begin(writeCtx)
	if err != nil {
		return fmt.Errorf("reextract: begin batch: %w", err)
	}
	defer func() {
		if tx != nil {
			_ = tx.Rollback()
		}
	}()
	for _, ev := range events {
		if _, err := outbox.Write(writeCtx, tx, ev); err != nil {
			// A failed outbox write is fatal to the batch: the sweep cannot claim
			// progress it did not durably emit.
			return fmt.Errorf("reextract: emit %s: %w", ev.RelPath, err)
		}
	}
	if err := index.SaveExtractStateTx(writeCtx, tx, next); err != nil {
		return err
	}
	c := tx
	tx = nil
	if err := c.Commit(); err != nil {
		return fmt.Errorf("reextract: commit batch: %w", err)
	}
	*state = next
	return nil
}

// withDefaults fills the zero knobs so a caller only has to express intent.
func (o Options) withDefaults() Options {
	if o.MaxItems <= 0 {
		o.MaxItems = DefaultMaxItems
	}
	if o.BatchSize <= 0 {
		o.BatchSize = DefaultBatchSize
	}
	if o.Log == nil {
		o.Log = slog.New(slog.NewTextHandler(io.Discard, nil))
	}
	if o.Now == nil {
		o.Now = time.Now
	}
	return o
}
