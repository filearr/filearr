package main

import (
	"context"
	"fmt"
	"github.com/filearr/filearr/agent/internal/agentlog"
	"log/slog"
	"net/http"
	"strings"
	"sync"
	"time"

	agentcfg "github.com/filearr/filearr/agent/internal/config"
	"github.com/filearr/filearr/agent/internal/enroll"
	"github.com/filearr/filearr/agent/internal/reconcile"
	"github.com/filearr/filearr/agent/internal/taxonomy"
)

// daemonApplier is the `run` daemon's Applier: it live-applies the honored policy
// keys to the running components. Scan-relevant keys (presets/globs/content
// ceiling) and watch_mode are consumed by reading the persisted policy.json at
// scan time (the poller persists them), so the only live wiring here is the
// reconcile-supervisor cadence.
type daemonApplier struct {
	sup *reconcile.Supervisor
	log *slog.Logger
	// ignored reports policy keys this host cannot honour, ONCE per distinct
	// change. It lives on the applier (not in ApplyPolicy) precisely so the
	// last-logged fingerprint survives across applies — an unfixable condition
	// like "no tesseract on this NAS" must be said clearly once, not on every
	// policy version bump forever.
	ignored *agentcfg.IgnoredLogger
	// updInterval forwards update_poll_interval_seconds to the self-updater.
	// The updater is constructed AFTER the poller (it needs to hand its
	// TriggerNow seam to the command poller), so this is a late-bound relay:
	// the value is kept and delivered when the updater binds.
	updInterval *intervalRelay
}

func (a *daemonApplier) ApplyPolicy(p agentcfg.Policy) error {
	if d, ok := p.ReconcileInterval(); ok {
		a.sup.SetInterval(d)
		a.log.Info("policy applied: reconcile interval", "interval", d.String())
	}
	// Central log_level (2026-08-20): group settings win when set; a bad name
	// is ignored (logged via the ignored-settings path would be overkill for a
	// validated-centrally field — central's Literal[] refuses typos at write).
	if p.Group != nil && p.Group.LogLevel != nil {
		if lvl, ok := agentlog.ParseLevel(*p.Group.LogLevel); ok {
			if prev := agentlog.SetLevel(lvl); prev != lvl {
				a.log.Info("policy applied: log level", "level", *p.Group.LogLevel)
			}
		}
	}
	if d, ok := p.UpdatePollInterval(); ok && a.updInterval != nil {
		if a.updInterval.Set(d) {
			a.log.Info("policy applied: update poll interval", "interval", d.String())
		}
	}
	if allowed, set := p.WatchAllowed(); set {
		a.log.Info("policy applied: watch_mode", "watch_mode", allowed)
	}
	// Extraction is consumed by the scan PROCESS (which re-reads the persisted
	// policy), so there is nothing live to reconfigure here — but this is the
	// seam that sees every policy change, and therefore the right place to tell
	// an operator which extraction settings will not apply on this host.
	a.ignored.Log(agentcfg.IgnoredSettings(p, hostExtractCapabilities()))
	return nil
}

// newPolicyClient builds the policy poll client against central, reusing the
// replicator's bearer-auth provider (interim cert-fingerprint scheme) and the
// shared mTLS-aware HTTP client (newHTTPClient; nil => client builds its own).
func newPolicyClient(certStore *enroll.CertStore, centralURL, agentID string, httpClient *http.Client) *agentcfg.PolicyClient {
	return agentcfg.NewPolicyClient(agentcfg.ClientConfig{
		BaseURL: centralURL,
		AgentID: agentID,
		AuthFn:  authProvider(certStore),
		HTTP:    httpClient,
		Logger:  newLogger(),
	})
}

// startPoller launches the policy poll loop for the `run` daemon alongside the
// renewer/replicator/supervisor. It returns a done-channel for a clean stop.
//
// W8-E: the poller also keeps the process-shared taxonomy cache fresh. After
// every successful poll it version-gates a taxonomy fetch off the policy's
// taxonomy_version, so an operator taxonomy edit (which bumps the policy ETag)
// propagates the compact taxonomy to <dataDir>/taxonomy.json — the same file the
// `scan` path reads. Refresh runs in a detached goroutine so the ~1271-entry
// fetch never blocks the poll loop.
func startPoller(ctx context.Context, dataDir string, certStore *enroll.CertStore, centralURL, agentID string, sup *reconcile.Supervisor, httpClient *http.Client, updInterval *intervalRelay) <-chan struct{} {
	taxCache := taxonomy.NewCache(dataDir, newLogger())
	taxClient := taxonomy.NewClient(taxonomy.ClientConfig{
		BaseURL: centralURL,
		AgentID: agentID,
		AuthFn:  authProvider(certStore),
		HTTP:    httpClient,
		Logger:  newLogger(),
	})
	poller := agentcfg.NewPoller(agentcfg.PollerConfig{
		Client:  newPolicyClient(certStore, centralURL, agentID, httpClient),
		Cache:   agentcfg.NewETagCache(dataDir),
		Applier: &daemonApplier{sup: sup, log: newLogger(), ignored: agentcfg.NewIgnoredLogger(newLogger()), updInterval: updInterval},
		Logger:  newLogger(),
		// Local "Sync with central" panel (2026-08-25).
		OnResult: syncStatus.reporter("policy"),
		AfterFetch: chainAfterFetch(
			taxonomyRefreshHook(taxCache, taxClient, newLogger()),
			// scan_selections -> scan.json on every successful poll, so a root
			// changed on central is live before the next scheduled scan.
			func(_ context.Context, _ agentcfg.PolicyDoc) { applyCentralScanRoots(dataDir, newLogger()) },
		),
	})
	done := make(chan struct{})
	go func() {
		defer close(done)
		if err := poller.Run(ctx); err != nil && ctx.Err() == nil {
			newLogger().Error("policy poll loop exited", "err", err)
		}
	}()
	return done
}

// chainAfterFetch runs several AfterFetch hooks in order (nil entries skipped).
func chainAfterFetch(hooks ...func(context.Context, agentcfg.PolicyDoc)) func(context.Context, agentcfg.PolicyDoc) {
	return func(ctx context.Context, doc agentcfg.PolicyDoc) {
		for _, h := range hooks {
			if h != nil {
				h(ctx, doc)
			}
		}
	}
}

// taxonomyRefreshHook returns a poller AfterFetch callback that version-gates a
// taxonomy refresh off the freshly-fetched policy's taxonomy_version (W8-E). The
// fetch runs in a detached goroutine so it never blocks the poll loop; a nil
// taxonomy_version (older central / never set) is a no-op.
func taxonomyRefreshHook(cache *taxonomy.Cache, client *taxonomy.Client, log *slog.Logger) func(context.Context, agentcfg.PolicyDoc) {
	return func(ctx context.Context, doc agentcfg.PolicyDoc) {
		pol, err := doc.Parsed()
		if err != nil {
			return
		}
		want := pol.TaxonomyVersionValue()
		if want <= cache.Version() {
			return
		}
		go func() {
			if err := cache.Refresh(ctx, client, want); err != nil {
				log.Warn("taxonomy refresh failed; keeping last-known taxonomy", "want", want, "err", err)
			}
		}()
	}
}

// runPolicy implements `filearr-agent policy [--fetch]`: without --fetch it
// prints the cached policy; with --fetch it does a one-shot poll+apply+persist
// against central (scripting/testing).
func runPolicy(args []string) error {
	fs := newFlagSet("policy")
	cfg := bindCommonFlags(fs)
	fetch := fs.Bool("fetch", false, "do a one-shot poll+apply against central (else print the cached policy)")
	if err := fs.Parse(args); err != nil {
		return err
	}

	cache := agentcfg.NewETagCache(cfg.DataDir)
	if !*fetch {
		doc, ok, err := cache.Load()
		if err != nil {
			return err
		}
		if !ok {
			fmt.Printf("no cached policy at %s (run `filearr-agent policy --fetch` or start the daemon)\n", cache.Path())
			return nil
		}
		printPolicyDoc(doc)
		return nil
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

	httpClient, err := newHTTPClient(certStore, centralURL)
	if err != nil {
		return err
	}
	// A one-shot CLI has no live daemon to reconfigure; NoopApplier still persists
	// the fetched policy so a subsequent `scan` honors central's scan settings.
	poller := agentcfg.NewPoller(agentcfg.PollerConfig{
		Client:  newPolicyClient(certStore, centralURL, st.AgentID, httpClient),
		Cache:   cache,
		Applier: agentcfg.NoopApplier{},
		Logger:  newLogger(),
	})

	ctx, cancel := signalContext()
	defer cancel()

	doc, outcome, err := poller.PollOnce(ctx)
	if err != nil {
		return err
	}
	printPolicyDoc(doc)
	switch outcome {
	case agentcfg.OutcomeApplied:
		fmt.Println("result: fetched new scope/version and applied")
	case agentcfg.OutcomeNotModified:
		fmt.Println("result: not modified (304, cache already current)")
	default: // OutcomeUnchanged
		fmt.Println("result: fetched, identity unchanged (no apply)")
	}
	return nil
}

// printPolicyDoc renders a cached/fetched policy document for the CLI.
func printPolicyDoc(doc agentcfg.PolicyDoc) {
	fetched := "never"
	if !doc.FetchedAt.IsZero() {
		fetched = doc.FetchedAt.Format(time.RFC3339)
	}
	fmt.Printf("scope=%s version=%d applied_version=%d etag=%s fetched_at=%s\n",
		doc.Scope, doc.Version, doc.AppliedVersion, doc.ETag, fetched)
	keys := doc.PolicyKeys()
	if len(keys) == 0 {
		fmt.Println("policy: (empty — defaults apply)")
		return
	}
	fmt.Printf("policy keys: %s\n", strings.Join(keys, ", "))
}

// intervalRelay carries a policy-set duration to a consumer that may not exist
// yet (the updater is built after the policy poller). Set stores the value and
// forwards it when bound; Bind delivers any value already received. Set reports
// whether the value changed (so the applier logs once per change, not per poll).
type intervalRelay struct {
	mu     sync.Mutex
	value  time.Duration
	target func(time.Duration)
}

func (r *intervalRelay) Set(d time.Duration) bool {
	r.mu.Lock()
	changed := d != r.value
	r.value = d
	t := r.target
	r.mu.Unlock()
	if t != nil && d > 0 {
		t(d)
	}
	return changed
}

func (r *intervalRelay) Bind(target func(time.Duration)) {
	r.mu.Lock()
	r.target = target
	d := r.value
	r.mu.Unlock()
	if d > 0 {
		target(d)
	}
}
