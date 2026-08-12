package main

// Daemon wiring for the `rehash_sweep` command (QH-T6, 2026-08-12).
//
// The defect: until 2026-07-18 QuickHash read a fixed 64 KiB head and only
// appended the tail above 131072 bytes, so a file in the 65537..131072 band had
// its middle and tail silently unhashed and two different files with matching
// headers collided. The hasher was fixed; the STORED values were not, and never
// will be by ordinary means — scan.diffEntry re-hashes only on a size/mtime
// change or an empty quick_hash, so a stable file in that band keeps its wrong
// hash forever. Central cannot help: it does not host these files, and
// agentsync never stamps policy_version on agent-owned rows, so its own
// provenance sweep cannot even TELL which rows are stale. 98,628 rows across
// seven libraries on the live fleet when this shipped.
//
// This file is the seam that hands internal/rehash the live hash policy and the
// daemon's pause state. Compare cmd/filearr-agent/reextract.go: same shape, and
// deliberately so.

import (
	"context"
	"log/slog"

	"github.com/filearr/filearr/agent/internal/index"
	"github.com/filearr/filearr/agent/internal/rehash"
	"github.com/filearr/filearr/agent/internal/scan"
)

// rehashSweepRunner builds the commands.Config.RunRehashSweep seam.
//
// The hash policy comes from the SAME place the scan process reads it — scan.json
// in the data dir, through the same hashPolicy() helper — and it is read at
// COMMAND time rather than at daemon start, for the same reason the re-extract
// runner re-reads the extraction policy: an operator's sequence is "fix the
// setting, then run the sweep", and a runner holding a snapshot from process
// start would apply rules the operator has already replaced. Sharing the helper
// also means the sweep inherits the per-file hash TIMEOUT the scan uses, which
// matters more here than anywhere: a poison file on a hung FUSE mount would
// otherwise wedge the sweep at the same item on every attempt, exactly the live
// Unraid shfs freeze the timeout was added for (2026-07-27).
//
// Unlike re-extract there is no policy that can DISABLE this work — a quick hash
// is always computed for every file — so an unreadable or absent scan.json is
// not a refusal, just a fall back to the local-by-construction default
// (ruling 6). Refusing would be worse than proceeding: the defect is real
// whether or not the config file parses.
func rehashSweepRunner(idx *index.Store, dataDir string, ops *opState, log *slog.Logger) func(context.Context, map[string]any) (map[string]any, error) {
	return func(ctx context.Context, payload map[string]any) (map[string]any, error) {
		// The zero scanConfig is the fallback, NOT scan.DefaultHashPolicy():
		// hashPolicy() is what applies the per-file hash timeout, and a policy
		// built without it is unbounded — precisely the wrong thing to hand a
		// sweep that will open tens of thousands of files on a mount that may
		// hang. The zero config only omits the content ceiling, which then falls
		// back to the same global default the raw constructor would have used.
		sc, err := readScanConfig(dataDir)
		if err != nil {
			sc = scanConfig{}
		}
		policy := hashPolicy(sc)

		minSize := int64(payloadInt(payload, "min_size"))
		maxSize := int64(payloadInt(payload, "max_size"))
		// Defaulting happens inside rehash.Run (a zero knob means "the defect
		// band"), but the FINGERPRINT has to be built from the SAME numbers the
		// sweep will actually use, or a default run and an explicit
		// 65537..131072 run would carry different fingerprints and each would
		// invalidate the other's cursor.
		if minSize <= 0 {
			minSize = rehash.DefaultMinSize
		}
		if maxSize <= 0 {
			maxSize = rehash.DefaultMaxSize
		}

		res, err := rehash.Run(ctx, idx, rehash.Options{
			Force:    payloadBool(payload, "force"),
			MaxItems: payloadInt(payload, "max_items"),
			MinSize:  minSize,
			MaxSize:  maxSize,
			// scan.HashSchemeVersion, not a literal: a future change to what the
			// hashers COMPUTE bumps that constant, every agent's fingerprint moves
			// with it, and the next sweep is real work again with no operator
			// action and no central-side coordination.
			FP: rehash.Fingerprint(scan.HashSchemeVersion, minSize, maxSize),
			// scan.HashFile, not QuickHash+FullHash spelled out here: the QH-T2
			// branch (a file at or below 131072 bytes always gets a real content
			// hash, regardless of policy) lives inside it, and a second copy of
			// that rule would make the sweep's output diverge from the scanner's
			// the first time either changed.
			Hash: func(absPath string, size int64) (string, string) {
				return scan.HashFile(absPath, size, policy)
			},
			// A sweep is ordinary replication work: it must stop while the agent is
			// suspended or central is in maintenance, for the same reason the
			// replicator does — the events it produces would only pile up. It is
			// also, unlike re-extract, a sustained I/O load on the host, which is
			// the other half of what "suspended" is supposed to mean.
			Paused: func() bool { return ops != nil && ops.ReplicationPaused() },
			Log:    log,
		})
		out := map[string]any{
			"seen":      res.Seen,
			"changed":   res.Changed,
			"verified":  res.Verified,
			"skipped":   res.Skipped,
			"failed":    res.Failed,
			"completed": res.Completed,
			"resumed":   res.Resumed,
			"cursor":    res.Cursor,
			"min_size":  res.MinSize,
			"max_size":  res.MaxSize,
		}
		if res.Reason != "" {
			out["reason"] = res.Reason
		}
		if err != nil {
			return out, err // partial counters still travel with the failure
		}
		return out, nil
	}
}
