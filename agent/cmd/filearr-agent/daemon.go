package main

import (
	"context"
	"crypto/x509"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"strings"

	"github.com/kardianos/service"

	"github.com/filearr/filearr/agent/internal/agentlog"
	"github.com/filearr/filearr/agent/internal/enroll"
	"github.com/filearr/filearr/agent/internal/install"
)

// runDaemon runs the long-lived daemon (certificate renewal + replication drain
// + reconcile supervisor + policy poller + local query API + web UI + command
// poller + thumbnailer + self-updater) UNDER kardianos service management.
//
// Wrapping the existing daemon logic in a service.Interface (rather than forking
// a second copy) is what makes `filearr-agent run` behave correctly in all three
// contexts from one code path: an interactive terminal (kardianos handles
// SIGINT/SIGTERM), a systemd/launchd unit, and the Windows SCM (which requires
// the process to answer the service control dispatcher — a bare console loop
// would be killed with "did not respond in a timely fashion"). The daemon body
// is unchanged; only its lifecycle owner is.
func runDaemon(args []string) error {
	fs := newFlagSet("run")
	cfg := bindCommonFlags(fs)
	socket := fs.String("socket", "", "override the local query API socket path (unix) / named-pipe name (windows); default is per-user")
	webAddr := fs.String("web-addr", envOr(envWebUIAddr, ""), "local web UI loopback bind address (host:port); default 127.0.0.1:8686 when the policy enables the web UI")
	if err := fs.Parse(args); err != nil {
		return err
	}

	if err := adoptionGuard(cfg.DataDir); err != nil {
		return err
	}

	// Fail fast BEFORE constructing the service so a misconfigured/unenrolled
	// host reports a clear error — but ONLY interactively. Under a service
	// manager every millisecond before svc.Run() delays connecting to the
	// Windows SCM dispatcher (30 s budget, event 7009 on overrun); Start()
	// performs the same load and returns the same error there.
	if service.Interactive() {
		store := enroll.NewCertStore(cfg.DataDir)
		if _, err := store.Load(); err != nil {
			return fmt.Errorf("no enrolled identity in %s (run `filearr-agent enroll` first): %w", cfg.DataDir, err)
		}
	}

	prog := &daemonProgram{cfg: cfg, socket: *socket, webAddr: *webAddr, log: newLogger()}
	svc, err := service.New(prog, install.RunServiceConfig())
	if err != nil {
		return fmt.Errorf("wire service manager: %w", err)
	}
	// Run blocks until the service manager (or an interactive interrupt) stops us;
	// it returns Start's error (fail-fast) or Stop's error (the daemon's run err).
	return svc.Run()
}

// daemonProgram adapts the daemon to kardianos service.Interface. Start wires and
// launches every loop and returns immediately (kardianos requires a non-blocking
// Start); Stop cancels the shared context and waits for a clean unwind.
type daemonProgram struct {
	cfg     *config
	socket  string
	webAddr string
	log     *slog.Logger

	cancel context.CancelFunc
	done   chan struct{}
	runErr error
}

// Start loads the identity and returns IMMEDIATELY; everything heavier —
// including opening the local index — runs in the p.run goroutine. Under the
// Windows SCM a service has ~30 s to leave START_PENDING, and openIndex can
// legitimately take minutes: the idempotent schema apply builds the report/
// search indexes over the whole items table on the first start after an
// upgrade (live 2026-08-04: a Windows service start died on exactly that
// with SCM timeout 7009). Only the cheap identity load stays synchronous so
// an unenrolled host still fails the start with a clear error.
func (p *daemonProgram) Start(s service.Service) error {
	store := enroll.NewCertStore(p.cfg.DataDir)
	id, err := store.Load()
	if err != nil {
		err = fmt.Errorf("no enrolled identity in %s (run `filearr-agent enroll` first): %w", p.cfg.DataDir, err)
		p.mirrorToSystemLog(s, err)
		return err
	}

	ctx, cancel := context.WithCancel(context.Background())
	p.cancel = cancel
	p.done = make(chan struct{})
	go p.run(ctx, s, store, id)
	return nil
}

// mirrorToSystemLog writes a fatal service error into the OS service log
// (Windows Event Log / syslog). A service has no terminal, and it may die
// before (or without) a file sink — without this mirror, a start failure
// surfaced only as the SCM's opaque "Incorrect function" event 7024 with no
// message anywhere (live 2026-08-04).
func (p *daemonProgram) mirrorToSystemLog(s service.Service, err error) {
	if s == nil {
		return
	}
	if sl, lerr := s.SystemLogger(nil); lerr == nil && sl != nil {
		_ = sl.Errorf("filearr-agent: %v", err)
	}
}

// failStart reports a fatal post-Start init failure. Before the async-init
// refactor this error returned from Start() and exited the process; the
// contract is preserved: log (file + OS service log) + nonzero exit (under a
// service manager that surfaces as a failed service and triggers any
// recovery policy).
func (p *daemonProgram) failStart(s service.Service, err error) {
	p.log.Error("daemon failed to start", "err", err)
	p.mirrorToSystemLog(s, err)
	os.Exit(1)
}

// adoptConfiguredCentralURL makes the RESOLVED config (flag > env > sidecar)
// authoritative over the enrollment-time state.json copy of central_url.
// Every daemon loop historically ran off id.State.CentralURL alone, so the
// documented mTLS-migration step — repoint the agent at agents.<domain> via
// sidecar/env/flag and restart — silently did NOTHING for an enrolled agent
// (live 2026-08-08: the sidecar said agents.<domain>, the daemon logged
// central=filearr.<domain>). A configured URL that differs from state is
// operator intent: persist it (best-effort) and run with it.
func adoptConfiguredCentralURL(cfg *config, store *enroll.CertStore, id *enroll.Identity, log *slog.Logger) {
	configured := strings.TrimRight(cfg.CentralURL, "/")
	if configured == "" || configured == strings.TrimRight(id.State.CentralURL, "/") {
		return
	}
	// The install one-liner writes the CONSOLE url into the sidecar, and central
	// may have redirected this agent to its mTLS agent-plane host at enrolment
	// (state.central_url != state.enroll_url). A sidecar/env still naming the
	// enrol url is the bootstrap value, not an operator repoint -- honouring it
	// would drag the agent back onto the host that refuses it. Only a url that
	// differs from BOTH is operator intent.
	if enrol := strings.TrimRight(id.State.EnrollURL, "/"); enrol != "" && configured == enrol {
		return
	}
	old := id.State.CentralURL
	id.State.CentralURL = configured
	if err := store.SaveState(id.State); err != nil {
		log.Warn("central URL switched by config but persisting state.json failed (in-memory switch still applies; will retry next start)",
			"err", err)
	}
	log.Info("central URL switched by config", "from", old, "to", configured)
}

// run performs the heavy initialization and hosts every daemon loop until the
// context is cancelled; its return closes p.done (what Stop waits on).
func (p *daemonProgram) run(ctx context.Context, s service.Service, store *enroll.CertStore, id *enroll.Identity) {
	defer close(p.done)

	// The operator's configured central URL (sidecar/env/flag) outranks the
	// enrollment-time copy — the seam the mTLS migration switches through.
	adoptConfiguredCentralURL(p.cfg, store, id, p.log)

	// The local index is opened even absent a prior scan (an empty outbox simply
	// drains to nothing). First start after an upgrade may apply schema/index
	// DDL over millions of rows — log so a long pause is diagnosable.
	p.log.Info("opening local index (an upgrade's first start may build indexes; this can take minutes)")
	idx, err := openIndex(p.cfg.DataDir)
	if err != nil {
		p.failStart(s, err)
		return
	}
	defer func() { _ = idx.Close() }()

	// The P7-T6 local query frecency store is a SEPARATE database from the index
	// (search history stays local, research §6). A failure is non-fatal.
	hist, herr := openHistory(p.cfg.DataDir)
	if herr != nil {
		p.log.Error("local query history disabled: cannot open history store", "err", herr)
		hist = nil
	}
	defer func() {
		if hist != nil {
			_ = hist.Close()
		}
	}()

	httpClient, err := newHTTPClient(store, id.State.CentralURL)
	if err != nil {
		p.failStart(s, err)
		return
	}

	// service.Interactive() is false only under an OS service manager. That is the
	// signal the self-updater needs to prefer clean-exit-for-restart over
	// self-re-exec after an A/B swap (a service manager races a re-exec).
	serviceManaged := !service.Interactive()
	agentlog.Verbose(p.log, "daemon starting",
		"agent_id", id.State.AgentID, "central", id.State.CentralURL, "service_managed", serviceManaged)

	renewer := &enroll.Renewer{
		Store:      store,
		CAURL:      id.State.CAURL,
		RootSHA256: id.State.CARootSHA256,
		Logger:     p.log,
	}

	// Credential-drift fix (2026-07-24): keep central's bound fingerprint in
	// step with the rotating leaf. Three triggers, one debounced rebinder:
	// after every renewal (the moment drift is born), once at startup (heals a
	// crash between renew and rebind AND every fleet already drifted before
	// this build), and from any loop's 401/403 (belt and braces).
	rebinder := &enroll.Rebinder{
		Store:   store,
		Central: &enroll.CentralClient{BaseURL: id.State.CentralURL, HTTP: httpClient},
		Logger:  p.log,
	}
	renewer.OnRenew = func(_ *x509.Certificate) { go rebinder.Trigger(ctx) }
	onAuthError := func() { go rebinder.Trigger(ctx) }
	go rebinder.Trigger(ctx)

	// Operational gates (2026-08-09): operator suspend (persisted) + central
	// maintenance advertisement. Created before the subsystems they gate.
	ops := newOpState(p.cfg.DataDir, p.log)

	sup, supDone := startSupervisor(ctx, idx, store, id.State.CentralURL, id.State.AgentID, httpClient)
	replDone := startReplication(ctx, idx, store, id.State.CentralURL, id.State.AgentID, sup, httpClient, onAuthError, ops.ReplicationPaused)
	// update_poll_interval_seconds relay: the poller may apply policy before the
	// updater exists; the relay hands the cadence over once the updater binds.
	updInterval := &intervalRelay{}
	pollDone := startPoller(ctx, p.cfg.DataDir, store, id.State.CentralURL, id.State.AgentID, sup, httpClient, updInterval)
	localDone := startLocalAPI(ctx, p.cfg.DataDir, p.socket, idx, hist)
	webDone := startWebUI(ctx, p.cfg, p.webAddr, idx, hist, ops)
	// The updater starts BEFORE the command poller so its TriggerNow seam can be
	// handed to the self_update command handler (console "update now" button).
	updTrigger, updDone := startUpdater(ctx, p.cfg.DataDir, store, id.State.CentralURL, id.State.AgentID, httpClient, serviceManaged, updInterval)
	cmdDone := startCommandPoller(ctx, idx, store, id.State.CentralURL, id.State.AgentID, httpClient, onAuthError, updTrigger, ops)
	thumbDone := startThumbnailer(ctx, idx, store, id.State.CentralURL, id.State.AgentID, httpClient)
	// Policy-driven scan scheduling (2026-08-03): the daemon runs scans itself
	// so a service-only install needs no external cron/Task Scheduler. Off
	// until policy (scan_cron/scan_interval_seconds/scan_on_start) or the
	// FILEARR_AGENT_SCAN_CRON/_EVERY/_ON_BOOT envs arm it.
	schedDone := startScanScheduler(ctx, p.cfg, p.log, ops.ScanHold)

	// slog (timestamped) — these used to be bare Printf lines, leaving the
	// container log's most important banner without a timestamp or level.
	p.log.Info("filearr-agent running",
		"version", Version, "agent_id", id.State.AgentID,
		"central", id.State.CentralURL,
		"cert_valid_until", id.Leaf.NotAfter.Format("2006-01-02T15:04:05Z07:00"))

	// Run blocks until ctx is cancelled (Stop / interrupt) and returns
	// ctx.Err(); let every sibling loop unwind cleanly before returning.
	err = renewer.Run(ctx)
	<-replDone
	<-supDone
	<-pollDone
	<-localDone
	<-webDone
	<-cmdDone
	<-thumbDone
	<-updDone
	<-schedDone
	if err != nil && !errors.Is(err, context.Canceled) {
		p.runErr = err
	}
}

// Stop cancels the daemon context and waits for a clean unwind. kardianos calls
// it on service stop or an interactive interrupt.
func (p *daemonProgram) Stop(s service.Service) error {
	if p.cancel != nil {
		p.cancel()
	}
	if p.done != nil {
		<-p.done
	}
	if p.runErr != nil {
		return p.runErr
	}
	p.log.Info("shutting down")
	return nil
}
