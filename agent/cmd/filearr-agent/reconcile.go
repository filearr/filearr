package main

import (
	"context"
	"fmt"
	"log/slog"
	"math/rand/v2"
	"net/http"
	"time"

	"github.com/filearr/filearr/agent/internal/enroll"
	"github.com/filearr/filearr/agent/internal/index"
	"github.com/filearr/filearr/agent/internal/outbox"
	"github.com/filearr/filearr/agent/internal/reconcile"
)

// newSweeper wires a reconcile Sweeper (protocol client + local store/outbox) for
// the given agent, reusing the replicator's bearer-auth provider and the shared
// mTLS-aware HTTP client (newHTTPClient; nil => client builds its own).
func newSweeper(idx *index.Store, certStore *enroll.CertStore, centralURL, agentID string, httpClient *http.Client) *reconcile.Sweeper {
	client := reconcile.NewClient(reconcile.ClientConfig{
		BaseURL: centralURL,
		AgentID: agentID,
		AuthFn:  authProvider(certStore),
		HTTP:    httpClient,
		Logger:  newLogger(),
	})
	return reconcile.NewSweeper(idx, outbox.New(idx.DB()), client, newLogger())
}

// runReconcile implements the one-shot `filearr-agent reconcile` (trigger d): a
// manual full-manifest sweep of every configured root, printing per-root counters.
func runReconcile(args []string) error {
	fs := newFlagSet("reconcile")
	cfg := bindCommonFlags(fs)
	if err := fs.Parse(args); err != nil {
		return err
	}

	certStore := enroll.NewCertStore(cfg.DataDir)
	st, err := certStore.LoadState()
	if err != nil {
		return fmt.Errorf("no enrolled identity in %s (run `filearr-agent enroll` first): %w", cfg.DataDir, err)
	}
	centralURL := cfg.CentralURL
	if centralURL == "" {
		centralURL = st.CentralURL
	}
	if centralURL == "" {
		return fmt.Errorf("central URL is required (-central, %s, or state.json)", envCentralURL)
	}

	idx, err := openIndex(cfg.DataDir)
	if err != nil {
		return err
	}
	defer idx.Close()

	ctx, cancel := signalContext()
	defer cancel()

	httpClient, err := newHTTPClient(certStore, centralURL)
	if err != nil {
		return err
	}
	sweeper := newSweeper(idx, certStore, centralURL, st.AgentID, httpClient)
	res, err := sweeper.Sweep(ctx, reconcile.Options{})
	printSweep(res)
	return err
}

// printSweep renders a SweepResult to stdout for the manual subcommand.
func printSweep(res reconcile.SweepResult) {
	if len(res.Roots) == 0 {
		fmt.Println("reconcile: no roots configured")
	}
	for _, rr := range res.Roots {
		switch {
		case rr.Err != nil:
			fmt.Printf("reconcile %s: error: %v\n", rr.LibraryRef, rr.Err)
		case rr.Matched:
			fmt.Printf("reconcile %s: match (rows=%d)\n", rr.LibraryRef, rr.RowCount)
		default:
			fmt.Printf("reconcile %s: reconciled rows=%d reset=%v %s\n",
				rr.LibraryRef, rr.RowCount, rr.Reset, rr.Finish.SortedCounters())
		}
	}
	if res.Reset {
		fmt.Printf("reconcile: outbox superseded (rebuilt=%v marked=%d)\n", res.Rebuilt, res.OutboxMarked)
	}
}

// startSupervisor builds the reconcile trigger Supervisor for the `run` daemon
// and launches its loop. It returns the Supervisor (registered as the
// replicator's Observer for triggers b/c) and a done-channel for a clean stop.
func startSupervisor(ctx context.Context, idx *index.Store, certStore *enroll.CertStore, centralURL, agentID string, httpClient *http.Client) (*reconcile.Supervisor, <-chan struct{}) {
	sweeper := newSweeper(idx, certStore, centralURL, agentID, httpClient)
	interval := reconcileInterval()
	sup := reconcile.NewSupervisor(sweeper.Sweep, interval, interval, newLogger())
	done := make(chan struct{})
	go func() {
		defer close(done)
		_ = sup.Run(ctx)
	}()
	go startupCatchup(ctx, idx, sup, interval, newLogger())
	return sup, done
}

// startupCatchupDelay is how long after daemon start the catch-up check waits
// (plus up to startupCatchupJitter) before triggering an overdue sweep — long
// enough for enrollment/replication to settle and to stay off the boot-time
// I/O rush, short enough that an overdue agent reconciles within minutes of
// starting rather than after 24h of uninterrupted uptime.
const (
	startupCatchupDelay  = 2 * time.Minute
	startupCatchupJitter = time.Minute
)

// startupCatchup fixes the "never reconciles" failure mode (live: agent XENON,
// 2026-08-22): the supervisor's periodic ticker starts from ZERO at process
// start, so a machine that sleeps, reboots, or self-updates more often than
// the interval never accumulates enough uptime to sweep — and the reconnect
// trigger needs a >interval outage that healthy replication never has. This
// reads the durable last-sweep watermark (store_flags, written by
// Sweeper.Sweep on success) and, when the last sweep is older than the
// interval — or never happened — schedules one through the supervisor's
// normal trigger path after a short jittered delay. The cadence thereby means
// "at most `interval` between sweeps" instead of "`interval` of uninterrupted
// uptime".
func startupCatchup(ctx context.Context, idx *index.Store, sup *reconcile.Supervisor, interval time.Duration, log *slog.Logger) {
	if interval <= 0 {
		return // periodic reconcile disabled: don't resurrect it at boot
	}
	delay := startupCatchupDelay + time.Duration(rand.Int64N(int64(startupCatchupJitter)))
	select {
	case <-ctx.Done():
		return
	case <-time.After(delay):
	}
	last, err := idx.LastReconcileAt(ctx)
	if err != nil {
		log.Warn("reconcile catch-up: could not read last-sweep watermark", "err", err)
		return
	}
	if !last.IsZero() && time.Since(last) < interval {
		log.Debug("reconcile catch-up: last sweep is fresh", "last", last.Format(time.RFC3339))
		return
	}
	if last.IsZero() {
		log.Info("reconcile catch-up: no recorded sweep; scheduling one")
	} else {
		log.Info("reconcile catch-up: last sweep overdue; scheduling one",
			"last", last.Format(time.RFC3339), "interval", interval.String())
	}
	sup.Trigger(false)
}

// reconcileResultMap flattens a SweepResult into the map a `reconcile` command
// posts back to central.
func reconcileResultMap(res reconcile.SweepResult) map[string]any {
	matched, reconciled, rows := 0, 0, 0
	var rootErrs []string
	for _, rr := range res.Roots {
		rows += rr.RowCount
		switch {
		case rr.Err != nil:
			rootErrs = append(rootErrs, fmt.Sprintf("%s: %v", rr.LibraryRef, rr.Err))
		case rr.Matched:
			matched++
		default:
			reconciled++
		}
	}
	out := map[string]any{
		"roots":      len(res.Roots),
		"matched":    matched,
		"reconciled": reconciled,
		"rows":       rows,
		"reset":      res.Reset,
	}
	if res.OutboxMarked > 0 {
		out["outbox_superseded"] = res.OutboxMarked
	}
	if len(rootErrs) > 0 {
		out["root_errors"] = rootErrs
	}
	return out
}
