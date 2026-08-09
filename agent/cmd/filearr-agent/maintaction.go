package main

// Local agent maintenance (2026-08-09): the `agent_maintenance` console
// command. Three passes, each best-effort — a failing pass records its error
// and the rest still run, so the command result always says what happened:
//
//  1. index compaction — sqlite VACUUM + WAL truncate on index.db (reclaims
//     space after large tombstone/purge churn);
//  2. outbox prune — delete replicated-and-acknowledged rows older than the
//     retention window (outbox.PruneSent keeps the newest row + all unsent);
//  3. temp sweep — remove stale atomic-write leftovers (.tmp-*) and
//     abandoned self-update downloads (download-*) from the data dir.

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/filearr/filearr/agent/internal/index"
	"github.com/filearr/filearr/agent/internal/outbox"
)

const (
	outboxPruneRetention = 7 * 24 * time.Hour
	tempSweepMinAge      = 24 * time.Hour
)

// runLocalMaintenance executes the three passes and returns the result map the
// command handler posts to central. The error return is non-nil only when
// EVERY pass failed — partial success is success with per-pass error keys.
func runLocalMaintenance(
	ctx context.Context, idx *index.Store, dataDir string, log *slog.Logger,
) (map[string]any, error) {
	t0 := time.Now()
	result := map[string]any{}
	failures := 0

	// 1. index compaction
	dbPath := filepath.Join(dataDir, indexDBName)
	if st, err := os.Stat(dbPath); err == nil {
		result["index_bytes_before"] = st.Size()
	}
	if _, err := idx.DB().ExecContext(ctx, "VACUUM"); err != nil {
		failures++
		result["index_error"] = fmt.Sprintf("vacuum: %v", err)
	} else {
		// Truncate the WAL too so the reclaimed space is visible on disk.
		_, _ = idx.DB().ExecContext(ctx, "PRAGMA wal_checkpoint(TRUNCATE)")
		result["index_vacuumed"] = true
	}
	if st, err := os.Stat(dbPath); err == nil {
		result["index_bytes_after"] = st.Size()
	}

	// 2. outbox prune
	if n, err := outbox.New(idx.DB()).PruneSent(ctx, outboxPruneRetention); err != nil {
		failures++
		result["outbox_error"] = err.Error()
	} else {
		result["outbox_pruned_rows"] = n
	}

	// 3. temp sweep (top-level data dir only — that is where atomicWrite and
	// the update downloader create their files; library content is never here)
	files, bytes, err := sweepTempFiles(dataDir, time.Now().Add(-tempSweepMinAge))
	if err != nil {
		failures++
		result["temp_error"] = err.Error()
	}
	result["temp_files_removed"] = files
	result["temp_bytes_removed"] = bytes

	result["duration_s"] = int(time.Since(t0).Seconds())
	log.Info("local maintenance pass finished",
		"duration", time.Since(t0).Round(time.Second).String(),
		"outbox_pruned", result["outbox_pruned_rows"],
		"temp_removed", files, "failures", failures)
	if failures >= 3 {
		return result, fmt.Errorf("all maintenance passes failed")
	}
	return result, nil
}

// sweepTempFiles removes stale temp artifacts directly under dir: atomic-write
// leftovers (".tmp-*" and "*.tmp") and abandoned update downloads
// ("download-*"), older than the cutoff. Returns count + bytes removed.
func sweepTempFiles(dir string, olderThan time.Time) (int, int64, error) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return 0, 0, err
	}
	var (
		files int
		bytes int64
	)
	for _, e := range entries {
		if e.IsDir() {
			continue
		}
		name := e.Name()
		if !strings.HasPrefix(name, ".tmp-") &&
			!strings.HasPrefix(name, "download-") &&
			!strings.HasSuffix(name, ".tmp") {
			continue
		}
		info, err := e.Info()
		if err != nil || info.ModTime().After(olderThan) {
			continue
		}
		if os.Remove(filepath.Join(dir, name)) == nil {
			files++
			bytes += info.Size()
		}
	}
	return files, bytes, nil
}
