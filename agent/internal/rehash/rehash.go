// Package rehash is the agent's operator-triggered quick_hash migration sweep
// (QH-T6, 2026-08-12).
//
// # Why it exists
//
// Until 2026-07-18 both hashers — Python's extract.quick_hash and this agent's
// scan.QuickHash, kept byte-for-byte identical on purpose — read a fixed 64 KiB
// head and appended a 64 KiB tail only when size > 131072. A file in the
// 65537..131072 band therefore had its middle and its tail SILENTLY UNHASHED:
// two genuinely different files whose first 64 KiB coincided (routine for
// JPEG/PNG/PDF headers and office-document boilerplate) produced the same
// quick_hash. That is a false duplicate in the reports, and a mis-keyed tier-1
// match in move detection. QH-T1 fixed the hashers in both languages.
//
// A fix to the hasher does not fix the stored values. scan.diffEntry re-hashes a
// file only when its size or mtime moved, or when its quick_hash is empty (the
// null-hash self-heal) — a deliberate design, because rewriting unchanged rows
// churns local_seq_no and floods replication. The consequence is that a stable
// file in the defect band keeps its wrong hash FOREVER: nothing about it will
// ever change, so nothing will ever look at it again.
//
// Central cannot repair these rows on the agent's behalf. It does not host the
// files, and agentsync.apply_batch never writes policy_version for agent-owned
// rows, so central's cfg1->cfg2 provenance sweep (the mechanism that converged
// its OWN catalogue, still_stale = 0 as of 2026-08-11) has no jurisdiction here.
// The agent is the only writer for those rows, so the agent is the only thing
// that can migrate them. Live scope when this shipped: 98,628 affected rows
// across seven libraries.
//
// # Relationship to internal/reextract
//
// Modelled on it almost line for line — same cursor discipline, same batch
// discipline, same idempotence rules — with ONE inversion. Re-extract skips a
// file whose size or mtime moved AND, for the files it does process, re-emits
// the index's stored identity verbatim because the bytes are unchanged. This
// sweep KEEPS the skip (a changed file is the ordinary scan's job, and repairing
// it here would push a fresh hash next to a stale size) but its entire purpose
// is to overwrite the identity fields, so it calls the hashers, writes the row
// through index.UpdateItem, and emits the NEW values.
//
// # What it must never do
//
// Attach an Extracted payload. Central's apply_batch merges metadata_ only when
// `extracted is not None`, and every applied batch defers a Meilisearch sync job
// — so attaching extraction here would turn a ~99k-row hash correction into a
// ~99k-item re-extraction and re-index across the fleet. Omitting the field is
// the whole containment. (Move detection needs no such care: it is agent-local,
// runs during the walk, and never runs on a replicated update.)
package rehash

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strconv"
	"time"

	"github.com/filearr/filearr/agent/internal/index"
	"github.com/filearr/filearr/agent/internal/outbox"
)

const (
	// DefaultMinSize / DefaultMaxSize are the DEFECT BAND, inclusive at both
	// ends, and they are a default rather than a limit.
	//
	// The lower edge is 65537 and not 1: the old code's unconditional
	// read(65536) naturally truncated at EOF, so a file of 65536 bytes or fewer
	// had its ENTIRE content hashed and its stored quick_hash is already
	// correct. Sweeping it would re-read ~1.03M files on the live fleet to
	// confirm digests that were never wrong.
	//
	// The upper edge is 131072 because that is where the tail branch started
	// firing under the old rules and still fires under the new ones — a file
	// above it was sampled head+tail then and is sampled head+tail now, so its
	// quick_hash is unchanged by QH-T1.
	//
	// Both are overridable per run. The wider <=131072-from-zero backfill (QH-T2
	// parity: granting content_hash to the small files that never had one) is a
	// legitimate thing to want, but it is ten times the I/O for a different
	// benefit, so it is a deliberate opt-in and never the default.
	DefaultMinSize int64 = 65537
	DefaultMaxSize int64 = 131072

	// DefaultMaxItems bounds how many candidates ONE command examines. Every
	// candidate here costs a stat plus a full read of up to 128 KiB, so unlike
	// the re-extract sweep the cost per item is nearly uniform and easy to
	// reason about: 250k items is at most ~32 GiB of reads. The default band
	// holds ~99k items fleet-wide, so in practice a single command covers an
	// entire agent and this bound only matters for a widened band.
	DefaultMaxItems = 250_000

	// DefaultBatchSize mirrors reextract.DefaultBatchSize and scan.flushEvery
	// (250): the same commit cadence, chosen for the same reason — it is the
	// granularity at which a crash loses work, and at which the single SQLite
	// write lock is taken and released.
	DefaultBatchSize = 250

	// commitTimeout bounds the detached batch commit. Purely a deadlock guard:
	// the write is pure-DB work against a store whose driver already carries a
	// 30s busy timeout.
	commitTimeout = 2 * time.Minute
)

// Stop reasons, reported verbatim in Result.Reason and posted back to central as
// part of the command result — an operator reads these, so they are sentences,
// not error codes.
const (
	reasonAlreadyDone = "already re-hashed at this scheme and size band"
	reasonCancelled   = "cancelled"
	reasonPaused      = "agent suspended or central in maintenance"
	reasonMaxItems    = "max_items reached"
)

// HashFn is the hashing seam, satisfied by scan.HashFile. Declared here rather
// than importing scan for the same reason reextract declares its own ExtractFn:
// the sweep packages stay free of the scan dependency and the daemon does the
// wiring (the posture commands.Config takes towards the update package).
//
// Contract, inherited from scan.hashFile: best-effort. An unreadable or hung
// file yields ("", "") rather than an error — a poison file must not fail the
// pass around it — and the sweep treats that as a FAILURE for that item, never
// as "the hash is now empty".
type HashFn func(absPath string, size int64) (quick, content string)

// Options configures one sweep.
type Options struct {
	Force    bool  // ignore the "already done at this scheme and band" short-circuit
	MaxItems int   // stop after this many candidates SEEN (<=0 => DefaultMaxItems)
	MinSize  int64 // inclusive lower band edge (<=0 => DefaultMinSize)
	MaxSize  int64 // inclusive upper band edge (<=0 => DefaultMaxSize)

	// FP is the sweep fingerprint (see Fingerprint). The caller builds it from
	// the SAME scheme and band it passes above; Run does not derive it, so a
	// daemon that wants to force a re-sweep for a reason the band cannot express
	// has somewhere to put that.
	FP string

	Hash      HashFn      // the hashing seam; nil => the sweep refuses to run
	Paused    func() bool // suspend / central-maintenance gate; nil => never paused
	BatchSize int         // items per transaction (<=0 => DefaultBatchSize)
	Log       *slog.Logger
	Now       func() time.Time
}

// Result reports what ONE run did. The counters are this run's, not the sweep's:
// MaxItems bounds a single command, so a chunked sweep reports each chunk here
// while the persisted index.RehashState accumulates the totals across chunks.
type Result struct {
	// Changed = rows this run actually corrected and emitted. Verified = rows it
	// re-read and found already right (an ordinary rescan had touched the file
	// since the QH-T1 binary landed, so the changed-file path repaired it first).
	// Kept apart because a sweep reporting "seen 40,000, changed 0, verified
	// 40,000" is a sweep confirming a converged agent, and a sweep reporting
	// "seen 40,000, changed 0" with no verified count is indistinguishable from
	// a broken one.
	Seen, Changed, Verified, Skipped, Failed int64

	Completed bool  // the band was walked to the end
	Resumed   bool  // this run continued a previous incomplete sweep
	Cursor    int64 // durable rowid frontier after this run

	// The band actually swept, after defaulting — echoed back so the command
	// result an operator reads states the range rather than implying it.
	MinSize, MaxSize int64

	Reason string // why it stopped, when it did not complete
}

// Fingerprint renders the sweep's INPUT RULES as a short stable string:
// "h<scheme>-<min>-<max>".
//
// Two runs with equal fingerprints would examine the same files and compute the
// same digests, which is precisely what makes the idempotence short-circuit
// safe. A differing fingerprint invalidates the cursor and re-sweeps from the
// beginning — which is the correct behaviour for both of the things that can
// change it: an operator widening the band (previously-excluded files must now
// be visited) and a future bump of scan.HashSchemeVersion (every stored digest
// in the band is stale again, fleet-wide, with no further operator action).
//
// Unhashed and human-readable on purpose, unlike reextract.Fingerprint's sha256
// digest — that one folds an unbounded tool map and needs collapsing, while this
// one has three small components and an operator comparing "h2-65537-131072" by
// eye on the console can tell at a glance which band an agent has migrated.
func Fingerprint(scheme int, minSize, maxSize int64) string {
	return "h" + strconv.Itoa(scheme) + "-" +
		strconv.FormatInt(minSize, 10) + "-" + strconv.FormatInt(maxSize, 10)
}

// Run performs one sweep (or one chunk of one). It returns an error only for a
// framing failure — an unusable band, a missing hashing seam, or a store
// read/write it cannot recover from; everything a single file can do to it is a
// skip or a failure count.
func Run(ctx context.Context, st *index.Store, opts Options) (Result, error) {
	opts = opts.withDefaults()
	if opts.Hash == nil {
		// A sweep with no hasher would walk the band, fail every item, and then
		// stamp the scheme as "done" — poisoning the short-circuit for the run an
		// operator will actually want. Refusing is the honest answer, and it
		// reaches the operator as the command's error.
		return Result{}, fmt.Errorf("rehash: no hashing seam wired on this agent build")
	}
	// Central validates the band too (422 before the command is ever queued), but
	// the agent refuses independently: a payload can also arrive from an older or
	// a hand-crafted caller, and an inverted band would walk zero rows and then
	// stamp the fingerprint FINISHED, permanently short-circuiting the real sweep
	// at that band until someone forces it.
	if opts.MinSize <= 0 || opts.MinSize > opts.MaxSize {
		return Result{}, fmt.Errorf(
			"rehash: invalid size band %d..%d (need 0 < min_size <= max_size)",
			opts.MinSize, opts.MaxSize)
	}

	stored, err := st.RehashState(ctx)
	if err != nil {
		return Result{}, err
	}

	var (
		res   = Result{MinSize: opts.MinSize, MaxSize: opts.MaxSize}
		state index.RehashState
	)
	switch {
	case !opts.Force && stored.FP == opts.FP && stored.FinishedAt != "":
		// Idempotence: this exact scheme and band have already been swept to the
		// end. A repeat command (a retry, a double click, a fleet-wide broadcast)
		// must not re-read a hundred thousand files to produce results central
		// already has.
		res.Completed = true
		res.Cursor = stored.CursorRowID
		res.Reason = reasonAlreadyDone
		return res, nil
	case !opts.Force && stored.FP == opts.FP:
		// Resume: same rules, unfinished sweep. This is what makes a
		// MaxItems-bounded run chunkable — the operator simply sends the command
		// again and it picks up at the durable cursor.
		state = stored
		res.Resumed = true
	default:
		// Invalidate: the band changed, the hash scheme changed, or the operator
		// forced it. Everything already swept was swept under rules that no longer
		// apply, so start over from rowid 0.
		state = index.RehashState{
			FP:        opts.FP,
			StartedAt: opts.Now().UTC().Format(time.RFC3339Nano),
			MinSize:   opts.MinSize,
			MaxSize:   opts.MaxSize,
		}
	}
	// The band is re-stamped even on a resume: the fingerprint already pins it, so
	// these two columns are a denormalised copy for the health block's benefit and
	// keeping them in step costs nothing.
	state.MinSize, state.MaxSize = opts.MinSize, opts.MaxSize
	// Persist the frontier before any work, so a run paused or cancelled before
	// its first batch still leaves the fingerprint and start time behind for the
	// next attempt to resume from.
	if err := st.SaveRehashState(ctx, state); err != nil {
		return res, err
	}

	maxItems := int64(opts.MaxItems)
	opts.Log.Info("quick_hash migration sweep starting",
		"fp", opts.FP, "resumed", res.Resumed, "cursor", state.CursorRowID,
		"min_size", opts.MinSize, "max_size", opts.MaxSize, "max_items", maxItems)
	started := opts.Now()

	stopReason := ""
	completed := false
	for {
		// Stop conditions are evaluated once per BATCH, not per item: Paused may
		// hit the daemon's state and MaxItems is a coarse bound, so paying for
		// either 250 times per commit would be waste. Cancellation is additionally
		// checked per item below, where an abandoned 128 KiB read is the thing
		// worth aborting.
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
		cands, err := st.RehashCandidates(ctx, state.CursorRowID, opts.MinSize, opts.MaxSize, limit)
		if err != nil {
			return res, err
		}
		// A short read means the index has no rows past the cursor inside the band:
		// the sweep is complete. Deciding it HERE (rather than on a following empty
		// query) lets the FinishedAt stamp ride the same transaction as the final
		// batch's writes.
		if len(cands) < limit {
			completed = true
		}

		// --- hashing phase: NO transaction open ------------------------------
		//
		// Same discipline as scan.Scan's flush() and reextract's: every slow thing
		// (a stat, and a full read of up to 128 KiB over a FUSE/SMB mount) happens
		// with the write lock released, and only the prepared writes execute inside
		// the batch transaction. Holding SQLite's single writer across a batch of
		// reads is precisely what starved the daemon's replicator into SQLITE_BUSY
		// backoffs during scans (live Unraid incident 2026-07-27).
		var (
			events  []outbox.Event
			updates []*index.Item
			batch   index.RehashState // per-batch counter deltas
			cursor  = state.CursorRowID
		)
		for _, c := range cands {
			if ctx.Err() != nil {
				stopReason = reasonCancelled
				break // the cursor stays behind this item: it is re-examined next run
			}
			cursor = c.RowID
			batch.Seen++

			it := c.Item
			abs := filepath.Join(c.RootPath, filepath.FromSlash(it.RelPath))
			info, statErr := os.Stat(abs)
			if statErr != nil || !info.Mode().IsRegular() {
				// Vanished, unreadable, or no longer a regular file. Not this pass's
				// problem: the next scan tombstones it (invariant 4), and a migration
				// sweep must never make that decision itself.
				batch.Skipped++
				continue
			}
			if info.Size() != it.Size || info.ModTime().UnixNano() != it.MtimeNs {
				// The bytes moved since the index last saw them. This is the guard
				// inherited unchanged from reextract, and it matters MORE here: the
				// ordinary scan will re-hash this file on its next pass anyway (that
				// is exactly the changed-file branch), so repairing it now would
				// duplicate that work while writing a fresh hash next to a size and
				// mtime this sweep is not allowed to update. The scan owns changed
				// files.
				batch.Skipped++
				continue
			}

			quick, content := opts.Hash(abs, it.Size)
			if quick == "" {
				// The seam swallowed an open/read error or hit the per-file hash
				// timeout (a hung FUSE mount, a locked file). Counted, named in the
				// log, and left alone — writing "" over a stored hash would destroy
				// a value that is merely SUSPECT in exchange for one that is
				// definitely absent, and would then look like a null-hash row the
				// scan's self-heal has to fix.
				batch.Failed++
				opts.Log.Warn("re-hash failed; row left as-is",
					"path", abs, "size", it.Size, "rel_path", it.RelPath)
				continue
			}
			// Never blank a stored content_hash. Inside the default band QH-T2
			// guarantees a content hash regardless of policy, so this cannot fire;
			// it can only fire on a widened band where the file is above the T7
			// ceiling or the policy declines content hashing. Keeping the stored
			// value is right in that case: it was computed when the policy did allow
			// it, and the file has not changed since (the stat above proved it).
			if content == "" && it.ContentHash != "" {
				content = it.ContentHash
			}
			if quick == it.QuickHash && content == it.ContentHash {
				// Already correct. Either an ordinary rescan touched this file after
				// the QH-T1 binary landed, or a previous run of this sweep covered it
				// and the cursor was later reset. No write, no event, no local_seq_no
				// churn, and nothing for central to re-index.
				batch.Verified++
				continue
			}

			it.QuickHash, it.ContentHash = quick, content
			// LastSeen is deliberately NOT touched. This is not a scan and it has
			// made no observation about the file's presence that tombstoning should
			// act on (tombstoning is driven by the walk's in-memory seen set, never
			// by last_seen); moving it would make the sweep look like a scan in every
			// report that reads that column.
			updates = append(updates, it)
			events = append(events, outbox.Event{
				ItemID:     it.ID,
				Op:         outbox.OpModified,
				LibraryRef: c.RootPath,
				RelPath:    it.RelPath,
				// Size and mtime are the INDEX's, unchanged — the stat above proved
				// they still describe the file. Only the two hash fields move.
				Size:        it.Size,
				MtimeNs:     it.MtimeNs,
				QuickHash:   it.QuickHash,
				ContentHash: it.ContentHash,
				// No ShareHint: this package has no share resolver, and central treats
				// an absent hint on a modified event as "no update", leaving a
				// previously-good hint intact. The scan owns hints.
				//
				// No Extracted, and this is the load-bearing omission of the whole
				// package — see the package doc. apply_batch merges metadata_ only
				// when extracted is present, so leaving it nil is what keeps a hash
				// correction from cascading into a fleet-wide re-extraction.
			})
			batch.Changed++
		}

		// A batch abandoned mid-flight has NOT reached the end of the band, even if
		// the short-read check above already guessed it would: the items past the
		// break were never examined, so the completion stamp must not ride this
		// commit.
		if stopReason != "" {
			completed = false
		}

		// --- commit phase: rows + events + cursor, one short transaction ------
		if err := commitBatch(ctx, st, updates, events, &state, cursor, batch, completed, opts.Now); err != nil {
			return res, err
		}
		res.Seen += batch.Seen
		res.Changed += batch.Changed
		res.Verified += batch.Verified
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
	opts.Log.Info("quick_hash migration sweep finished",
		"seen", res.Seen, "changed", res.Changed, "verified", res.Verified,
		"skipped", res.Skipped, "failed", res.Failed,
		"completed", res.Completed, "reason", res.Reason, "cursor", res.Cursor,
		"duration", opts.Now().Sub(started).Round(time.Second).String())
	return res, nil
}

// commitBatch writes one batch's corrected item rows, its outbox events and the
// advanced cursor in a SINGLE transaction, so a crash resumes exactly where the
// last durable write landed: either all three are visible or none is.
//
// The UpdateItem calls come FIRST and the outbox.Write for each row second,
// mirroring the scan's per-item ordering (index write, then emit) — not because
// SQLite cares about the order inside one transaction, but because the invariant
// worth being able to state is "no event exists that its row does not back".
//
// The transaction runs on a ctx detached from cancellation (bounded by
// commitTimeout) for the same reason outbox.MarkSent does: the expensive part —
// re-reading up to 128 KiB per file over a network mount — is already paid for,
// and losing a whole batch of it to a shutdown racing the commit would mean
// re-reading those files on the next run. The loop above is what honours
// cancellation; this write deliberately finishes.
func commitBatch(
	ctx context.Context, st *index.Store, updates []*index.Item, events []outbox.Event,
	state *index.RehashState, cursor int64, batch index.RehashState,
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
	next.Changed += batch.Changed
	next.Verified += batch.Verified
	next.Skipped += batch.Skipped
	next.Failed += batch.Failed
	if completed {
		next.FinishedAt = now().UTC().Format(time.RFC3339Nano)
	}

	writeCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), commitTimeout)
	defer cancel()

	tx, err := st.Begin(writeCtx)
	if err != nil {
		return fmt.Errorf("rehash: begin batch: %w", err)
	}
	defer func() {
		if tx != nil {
			_ = tx.Rollback()
		}
	}()
	for _, it := range updates {
		if err := index.UpdateItem(writeCtx, tx, it); err != nil {
			return fmt.Errorf("rehash: update %s: %w", it.RelPath, err)
		}
	}
	for _, ev := range events {
		if _, err := outbox.Write(writeCtx, tx, ev); err != nil {
			// A failed outbox write is fatal to the batch: the sweep cannot claim
			// progress it did not durably emit, and the corrected row above it would
			// otherwise be a local repair central never hears about — invisible
			// divergence, the worst outcome available here.
			return fmt.Errorf("rehash: emit %s: %w", ev.RelPath, err)
		}
	}
	if err := index.SaveRehashStateTx(writeCtx, tx, next); err != nil {
		return err
	}
	c := tx
	tx = nil
	if err := c.Commit(); err != nil {
		return fmt.Errorf("rehash: commit batch: %w", err)
	}
	*state = next
	return nil
}

// withDefaults fills the zero knobs so a caller only has to express intent.
func (o Options) withDefaults() Options {
	if o.MaxItems <= 0 {
		o.MaxItems = DefaultMaxItems
	}
	if o.MinSize <= 0 {
		o.MinSize = DefaultMinSize
	}
	if o.MaxSize <= 0 {
		o.MaxSize = DefaultMaxSize
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
