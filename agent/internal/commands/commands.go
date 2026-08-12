package commands

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"math/rand"
	"net/http"
	"strings"
	"time"

	"github.com/filearr/filearr/agent/internal/inventory"
)

// Defaults (mirroring the central caps in config.py). The poll cap is clamped
// server-side to FILEARR_AGENT_COMMAND_POLL_MAX (50); the lease default matches
// FILEARR_AGENT_COMMAND_LEASE_SECONDS (300s).
const (
	defaultMaxCommands  = 10
	defaultInterval     = 60 * time.Second
	defaultLeaseSeconds = 300
	defaultTimeout      = 60 * time.Second
	maxBackoff          = 5 * time.Minute
)

// Config configures a Poller; zero-valued fields take the defaults above.
type Config struct {
	BaseURL string
	AgentID string

	// AuthFn returns the per-request bearer token (the agent cert fingerprint),
	// exactly as the replicator/reconcile clients use. Called per-request.
	AuthFn func() string

	HTTP     *http.Client
	Executor *Executor

	// RateProvider returns the per-agent staging-upload rate cap (bytes/sec, 0 =
	// unlimited) from the cached central policy, read at the START of each
	// stage_upload (P10-T4). Nil => unlimited. A mid-upload policy change applies
	// on the next upload (documented).
	RateProvider func() int64

	// Inventory runs W6-D3 `inventory` commands. Nil => the inventory kind is
	// unsupported (completed ok=false with a note), so an older agent build degrades
	// cleanly against a central that enqueues one.
	Inventory *inventory.Runner

	// Capabilities produces the additive advertisement attached to EVERY poll
	// body (inventory collector vocabulary + version, host-tool matrix with
	// versions and resolved paths, build provenance); central stores it on the
	// agent row so the UI can offer only composable collectors and render the
	// per-agent About view. Nil, or a nil/empty return, => omitted.
	//
	// A FUNCTION rather than a snapshot (2026-08-11), mirroring Health. It was
	// a map, evaluated once when the daemon wired this poller, and that made
	// the advertisement a statement about the agent's BOOT — a host tool
	// installed afterwards could never appear, because nothing re-asked. The
	// operator-visible symptom was an exiftool that was demonstrably installed
	// and permanently reported absent, curable only by restarting the service.
	// Now it is re-evaluated per poll and the tool caches carry their own TTL.
	//
	// Must stay cheap: the underlying caches make the steady-state cost a few
	// map reads, with one round of LookPath + version probes per TTL.
	Capabilities func() map[string]any

	// Health, when non-nil, produces the compact self-reported health snapshot
	// (uptime, outbox backlog, index size, scan state) attached to every poll
	// body under the same contract as Capabilities: stored VERBATIM on the
	// agent row (size-capped centrally), older centrals ignore it. Must be
	// cheap and never block long — a nil/empty return omits the field.
	Health func(ctx context.Context) map[string]any

	// Version is this binary's running version (main.Version), attached to
	// every poll body so central's ``agents.agent_version`` stays current for
	// agents whose self-update subsystem is OFF (the container image disables
	// it, so the update-manifest poll — the historical version-confirmation
	// channel — never runs there and central showed the enrollment-era
	// version forever; live 2026-08-08). Empty => omitted.
	Version string

	// MaxCommands drained per poll (default 10); Interval between polls (default
	// 60s); LeaseSeconds is the picked_up lease whose third is the ack-heartbeat
	// cadence during a slow content hash (default 300 -> heartbeat every ~100s).
	MaxCommands  int
	Interval     time.Duration
	LeaseSeconds int

	Logger *slog.Logger
	// Clock/Rand are injectable for deterministic tests (nil => time.Now / a
	// package rand source).
	Clock func() time.Time
	Rand  *rand.Rand

	// OnAuthError, if set, fires when central rejects the agent credential
	// (401/403) — wired to the cert-rebind trigger so a drifted fingerprint
	// self-heals (fix 2026-07-24). Must not block; the rebinder debounces.
	OnAuthError func()

	// TriggerUpdate runs one immediate self-update check-and-apply on behalf of
	// a self_update command (update.Updater.TriggerNow's exact signature —
	// declared structurally to keep this package update-free). beforeApply is
	// where the handler posts its command result: a successful apply exits the
	// process. Nil => the kind completes ok=false ("unavailable"), so an agent
	// with self-update disabled degrades cleanly.
	TriggerUpdate func(ctx context.Context, beforeApply func(version string)) (string, bool, error)

	// OnMaintenance, if set, receives central's maintenance-mode advertisement
	// after every successful poll (the X-Filearr-Maintenance response header:
	// present+"1" while active, absent otherwise). The daemon wires it to the
	// replication pause gate so the agent stops pushing updates while central
	// is under maintenance — local scanning/inventory continues. Called with
	// the CURRENT value on every poll (not edge-triggered); must be cheap.
	OnMaintenance func(active bool)

	// SetSuspended applies a `suspend` command: persist the flag and gate the
	// agent's own scan scheduling + replication push. Nil => the kind completes
	// ok=false ("unavailable"). Must be idempotent.
	SetSuspended func(ctx context.Context, suspended bool) error

	// RunMaintenance applies an `agent_maintenance` command: local index
	// VACUUM, outbox prune, temp-file sweep. Returns the result map posted to
	// central (bytes reclaimed etc.). Nil => the kind completes ok=false.
	RunMaintenance func(ctx context.Context) (map[string]any, error)

	// RunReextract applies a `reextract` command: sweep the existing local index
	// and re-emit items with a fresh extraction result (agent parity phase 3 —
	// extraction otherwise only ever runs over files a scan reports as new or
	// changed, so items catalogued before extraction was enabled, or before the
	// host gained a tool, are never enriched). Takes the command payload
	// verbatim ({"force": bool, "max_items": int}) and returns the counters
	// posted back to central. Nil => the kind completes ok=false, so an older
	// agent degrades cleanly against a central that enqueues one.
	RunReextract func(ctx context.Context, payload map[string]any) (map[string]any, error)

	// RunRehashSweep applies a `rehash_sweep` command (QH-T6): re-read every
	// indexed file in a size band, recompute its hashes under the post-QH-T1
	// rules, and re-emit the ones whose stored value was wrong. Distinct from
	// the `rehash_check` KIND, which verifies ONE item and writes nothing —
	// see rehash_sweep.go. Takes the command payload verbatim ({"force",
	// "max_items", "min_size", "max_size"}) and returns the counters posted
	// back to central. Nil => the kind completes ok=false, so an older agent
	// degrades cleanly against a central that enqueues one.
	RunRehashSweep func(ctx context.Context, payload map[string]any) (map[string]any, error)
}

// Poller drains central's per-agent command queue and executes each command.
type Poller struct {
	baseURL        string
	agentID        string
	authFn         func() string
	http           *http.Client
	exec           *Executor
	rateProvider   func() int64
	inv            *inventory.Runner
	caps           func() map[string]any
	health         func(ctx context.Context) map[string]any
	version        string
	maxCmds        int
	interval       time.Duration
	leaseSecs      int
	log            *slog.Logger
	clock          func() time.Time
	rnd            *rand.Rand
	onAuthError    func()
	updateTrigger  func(ctx context.Context, beforeApply func(version string)) (string, bool, error)
	onMaintenance  func(active bool)
	setSuspended   func(ctx context.Context, suspended bool) error
	runMaint       func(ctx context.Context) (map[string]any, error)
	runReextract   func(ctx context.Context, payload map[string]any) (map[string]any, error)
	runRehashSweep func(ctx context.Context, payload map[string]any) (map[string]any, error)
}

// NewPoller wires a Poller, applying defaults.
func NewPoller(cfg Config) *Poller {
	p := &Poller{
		baseURL:        strings.TrimRight(cfg.BaseURL, "/"),
		agentID:        cfg.AgentID,
		authFn:         cfg.AuthFn,
		http:           cfg.HTTP,
		exec:           cfg.Executor,
		rateProvider:   cfg.RateProvider,
		inv:            cfg.Inventory,
		caps:           cfg.Capabilities,
		health:         cfg.Health,
		version:        cfg.Version,
		maxCmds:        cfg.MaxCommands,
		interval:       cfg.Interval,
		leaseSecs:      cfg.LeaseSeconds,
		log:            cfg.Logger,
		clock:          cfg.Clock,
		rnd:            cfg.Rand,
		onAuthError:    cfg.OnAuthError,
		updateTrigger:  cfg.TriggerUpdate,
		onMaintenance:  cfg.OnMaintenance,
		setSuspended:   cfg.SetSuspended,
		runMaint:       cfg.RunMaintenance,
		runReextract:   cfg.RunReextract,
		runRehashSweep: cfg.RunRehashSweep,
	}
	if p.http == nil {
		p.http = &http.Client{Timeout: defaultTimeout}
	}
	if p.maxCmds <= 0 {
		p.maxCmds = defaultMaxCommands
	}
	if p.interval <= 0 {
		p.interval = defaultInterval
	}
	if p.leaseSecs <= 0 {
		p.leaseSecs = defaultLeaseSeconds
	}
	if p.log == nil {
		p.log = slog.New(slog.NewTextHandler(io.Discard, nil))
	}
	if p.clock == nil {
		p.clock = time.Now
	}
	if p.rnd == nil {
		p.rnd = rand.New(rand.NewSource(time.Now().UnixNano()))
	}
	if p.authFn == nil {
		p.authFn = func() string { return "" }
	}
	return p
}

// commandOut is the subset of central's CommandOut the agent consumes. The rest
// (status/attempts/timestamps) is ignored here.
type commandOut struct {
	ID      string         `json:"id"`
	Kind    string         `json:"kind"`
	ItemID  string         `json:"item_id"`
	Payload map[string]any `json:"payload"`
}

// Run polls until ctx is cancelled: a plain poll every Interval (±10% jitter),
// backing off (capped) while central is unreachable and resetting on the first
// success. A shutdown between polls is clean; a command mid-execution finishes
// (its complete uses a detached ctx) or is redelivered by central's sweep.
func (p *Poller) Run(ctx context.Context) error {
	backoff := time.Duration(0)
	for {
		if err := ctx.Err(); err != nil {
			return err
		}
		_, err := p.PollOnce(ctx)
		var wait time.Duration
		if err != nil {
			if backoff == 0 {
				backoff = p.interval
			} else {
				backoff *= 2
			}
			if backoff > maxBackoff {
				backoff = maxBackoff
			}
			p.log.Warn("command poll failed; backing off", "backoff", backoff.String(), "err", err)
			wait = backoff
		} else {
			backoff = 0
			wait = p.jittered(p.interval)
		}
		if !sleepCtx(ctx, wait) {
			return ctx.Err()
		}
	}
}

// PollOnce drains one poll of commands and processes each. It returns the number
// of commands processed and an error ONLY when the poll request itself failed
// (central-down / non-200) — a per-command execute/complete failure is logged and
// never aborts the batch (the sweep redelivers an un-completed command).
func (p *Poller) PollOnce(ctx context.Context) (int, error) {
	cmds, err := p.poll(ctx)
	if err != nil {
		return 0, err
	}
	for _, cmd := range cmds {
		p.process(ctx, cmd)
	}
	return len(cmds), nil
}

// process executes one command and reports its terminal result. Unknown/
// unsupported kinds complete ok=false with a note (never left dangling).
func (p *Poller) process(ctx context.Context, cmd commandOut) {
	switch cmd.Kind {
	case KindStatCheck, KindRehashCheck:
		p.processVerify(ctx, cmd)
	case KindStageUpload:
		p.processStageUpload(ctx, cmd)
	case KindInventory:
		p.processInventory(ctx, cmd)
	case KindSelfUpdate:
		p.processSelfUpdate(ctx, cmd)
	case KindSuspend:
		p.processSuspend(ctx, cmd)
	case KindAgentMaintenance:
		p.processAgentMaintenance(ctx, cmd)
	case KindReextract:
		p.processReextract(ctx, cmd)
	case KindRehashSweep:
		p.processRehashSweep(ctx, cmd)
	default:
		p.complete(ctx, cmd.ID, false, map[string]any{"error": fmt.Sprintf("unknown command kind %q", cmd.Kind)})
	}
}

// processVerify runs a stat_check / rehash_check, heartbeating the lease during a
// (potentially slow) rehash so central's redelivery sweep does not reclaim it.
func (p *Poller) processVerify(ctx context.Context, cmd commandOut) {
	var (
		res   CommandResult
		exErr error
	)
	if cmd.Kind == KindRehashCheck {
		// A big content hash can outlast the lease: heartbeat every lease/3 while
		// it runs. stat_check is a single stat and never needs it.
		hbCtx, cancel := context.WithCancel(ctx)
		go p.heartbeat(hbCtx, cmd.ID)
		res, exErr = p.exec.Execute(ctx, cmd.Kind, cmd.Payload)
		cancel()
	} else {
		res, exErr = p.exec.Execute(ctx, cmd.Kind, cmd.Payload)
	}

	if exErr != nil {
		// "Cannot answer" (unknown root / traversal / IO error): fail the command
		// so central does not reconcile a wrong answer.
		p.log.Warn("verify command refused", "command_id", cmd.ID, "kind", cmd.Kind, "err", exErr)
		p.complete(ctx, cmd.ID, false, map[string]any{"error": exErr.Error()})
		return
	}
	p.complete(ctx, cmd.ID, true, resultMap(res))
}

// heartbeat acks the command every lease/3 until ctx is cancelled (the execute
// returns). ack failures are logged, never fatal.
func (p *Poller) heartbeat(ctx context.Context, commandID string) {
	interval := time.Duration(p.leaseSecs) * time.Second / 3
	if interval <= 0 {
		interval = time.Second
	}
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			if err := p.ack(context.WithoutCancel(ctx), commandID); err != nil {
				p.log.Warn("command lease heartbeat (ack) failed", "command_id", commandID, "err", err)
			}
		}
	}
}

// --- transport -------------------------------------------------------------

func (p *Poller) poll(ctx context.Context) ([]commandOut, error) {
	url := fmt.Sprintf("%s/api/v1/agents/%s/commands/poll", p.baseURL, p.agentID)
	reqBody := map[string]any{"max": p.maxCmds}
	if p.caps != nil {
		// Additive capability advertisement — central persists it on the agent
		// row. An older central that ignores the field is unaffected. Rebuilt
		// per poll (see Config.Capabilities) so a host change reaches the
		// console without a service restart; an empty return omits the field
		// rather than overwriting a good stored advertisement with `{}`.
		if c := p.caps(); len(c) > 0 {
			reqBody["capabilities"] = c
		}
	}
	if p.health != nil {
		// Self-reported health snapshot: same additive contract as capabilities.
		if h := p.health(ctx); len(h) > 0 {
			reqBody["health"] = h
		}
	}
	if p.version != "" {
		// Version confirmation for agents whose update poll never runs
		// (self-update disabled, e.g. the container image).
		reqBody["version"] = p.version
	}
	status, hdr, body, err := p.postHdr(ctx, url, reqBody)
	if err != nil {
		return nil, err
	}
	if status != http.StatusOK {
		return nil, p.statusError("poll", status, body)
	}
	// Central maintenance advertisement (2026-08-09): the response body is a
	// frozen bare array, so the mode rides a response header. Level-triggered:
	// the callback gets the CURRENT value every successful poll, so a missed
	// deactivation self-heals on the next one.
	if p.onMaintenance != nil {
		p.onMaintenance(hdr.Get("X-Filearr-Maintenance") == "1")
	}
	var out []commandOut
	if err := json.Unmarshal(body, &out); err != nil {
		return nil, fmt.Errorf("poll: decode body: %w", err)
	}
	return out, nil
}

func (p *Poller) ack(ctx context.Context, commandID string) error {
	url := fmt.Sprintf("%s/api/v1/agents/%s/commands/%s/ack", p.baseURL, p.agentID, commandID)
	status, body, err := p.post(ctx, url, nil)
	if err != nil {
		return err
	}
	if status != http.StatusOK {
		return p.statusError("ack", status, body)
	}
	return nil
}

// complete reports the terminal result. It uses a detached ctx so a shutdown
// racing the report still records it (mirrors the replicator's MarkSent posture).
func (p *Poller) complete(ctx context.Context, commandID string, ok bool, result map[string]any) {
	url := fmt.Sprintf("%s/api/v1/agents/%s/commands/%s/complete", p.baseURL, p.agentID, commandID)
	body := map[string]any{"ok": ok, "result": result}
	status, resp, err := p.post(context.WithoutCancel(ctx), url, body)
	if err != nil {
		p.log.Warn("command complete failed", "command_id", commandID, "err", err)
		return
	}
	if status != http.StatusOK {
		p.log.Warn("command complete rejected", "command_id", commandID, "err", p.statusError("complete", status, resp))
	}
}

func (p *Poller) post(ctx context.Context, url string, body any) (int, []byte, error) {
	status, _, respBody, err := p.postHdr(ctx, url, body)
	return status, respBody, err
}

// postHdr is post with the response headers exposed (the poll needs the
// X-Filearr-Maintenance advertisement).
func (p *Poller) postHdr(ctx context.Context, url string, body any) (int, http.Header, []byte, error) {
	var reader io.Reader
	if body != nil {
		buf, err := json.Marshal(body)
		if err != nil {
			return 0, nil, nil, fmt.Errorf("marshal body: %w", err)
		}
		reader = bytes.NewReader(buf)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, reader)
	if err != nil {
		return 0, nil, nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	if tok := p.authFn(); tok != "" {
		req.Header.Set("Authorization", "Bearer "+tok)
	}
	resp, err := p.http.Do(req)
	if err != nil {
		return 0, nil, nil, err
	}
	defer resp.Body.Close()
	respBody, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	return resp.StatusCode, resp.Header, respBody, nil
}

func (p *Poller) statusError(step string, status int, body []byte) error {
	detail := centralDetail(body)
	switch status {
	case http.StatusNotFound:
		return fmt.Errorf("commands %s: 404 — agent-command feature disabled or command gone: %s", step, detail)
	case http.StatusUnauthorized, http.StatusForbidden:
		if p.onAuthError != nil {
			p.onAuthError()
		}
		return fmt.Errorf("commands %s: central rejected the agent bearer token (%d): %s", step, status, detail)
	default:
		return fmt.Errorf("commands %s: central returned %d: %s", step, status, detail)
	}
}

// jittered returns d ±10% so a fleet of agents does not poll in lockstep.
func (p *Poller) jittered(d time.Duration) time.Duration {
	if d <= 0 {
		return d
	}
	delta := float64(d) * 0.1
	return d + time.Duration((p.rnd.Float64()*2-1)*delta)
}

// resultMap converts a CommandResult to the map central's complete endpoint
// stores as the row's result JSONB (== the CommandResult contract).
func resultMap(r CommandResult) map[string]any {
	m := map[string]any{"exists": r.Exists, "content_skipped": r.ContentSkipped}
	if r.Size != nil {
		m["size"] = *r.Size
	}
	if r.Mtime != nil {
		m["mtime"] = *r.Mtime
	}
	if r.QuickHash != nil {
		m["quick_hash"] = *r.QuickHash
	}
	if r.ContentHash != nil {
		m["content_hash"] = *r.ContentHash
	}
	return m
}

// centralDetail unwraps a FastAPI {"detail": ...} envelope for logging.
func centralDetail(body []byte) string {
	var env struct {
		Detail json.RawMessage `json:"detail"`
	}
	if json.Unmarshal(body, &env) == nil && len(env.Detail) > 0 {
		var s string
		if json.Unmarshal(env.Detail, &s) == nil && s != "" {
			return s
		}
		return string(env.Detail)
	}
	if len(body) > 512 {
		body = body[:512]
	}
	return string(body)
}

// sleepCtx sleeps for d or until ctx is cancelled; false => cancelled first.
func sleepCtx(ctx context.Context, d time.Duration) bool {
	if d <= 0 {
		select {
		case <-ctx.Done():
			return false
		default:
			return true
		}
	}
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-t.C:
		return true
	}
}
