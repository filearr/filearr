package enroll

import (
	"context"
	"crypto"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"errors"
	"fmt"
	"log/slog"
	"strconv"
	"sync"
	"time"
)

// Rebind — proof-of-possession fingerprint rotation (credential-drift fix
// 2026-07-24).
//
// The bug this cures: in central's default `fingerprint` auth mode the bearer
// token IS the current leaf's SHA-256, but central only learned that value once
// (at enrollment). The renewal daemon rotates the leaf at ~2/3 lifetime, so
// ~32h after enroll every request starts 401ing — the caveat documented on
// authProvider (cmd/filearr-agent/replicate.go). POST /agents/{id}/rebind lets
// the agent prove possession of a CA-issued cert for its own id (chain to the
// pinned root + SAN match + a signature with the leaf key, which SURVIVES
// renewal) and rotate the stored binding — no bearer needed, which is the
// point: the bearer is exactly what has drifted.

// rebindContext is the domain-separation prefix baked into the signed payload.
// It MUST match backend/filearr/agentcert.py REBIND_CONTEXT byte-for-byte —
// pinned by a cross-language vector test on each side.
const rebindContext = "filearr-agent-rebind-v1"

// RebindPayload builds the canonical bytes both sides sign/verify:
// context \n agent_id \n unix_timestamp \n leaf_fingerprint.
func RebindPayload(agentID string, timestamp int64, fingerprint string) []byte {
	return []byte(rebindContext + "\n" + agentID + "\n" +
		strconv.FormatInt(timestamp, 10) + "\n" + fingerprint)
}

// SignRebind signs the canonical payload with the agent's leaf key (ECDSA
// P-256; SHA-256 digest, ASN.1 DER signature) and returns it base64-encoded.
func SignRebind(key crypto.PrivateKey, payload []byte) (string, error) {
	signer, ok := key.(crypto.Signer)
	if !ok {
		return "", errors.New("agent key does not implement crypto.Signer")
	}
	digest := sha256.Sum256(payload)
	sig, err := signer.Sign(rand.Reader, digest[:], crypto.SHA256)
	if err != nil {
		return "", fmt.Errorf("sign rebind payload: %w", err)
	}
	return base64.StdEncoding.EncodeToString(sig), nil
}

// RebindRequest mirrors backend RebindIn.
type RebindRequest struct {
	CertChainPEM string `json:"cert_chain_pem"`
	Timestamp    int64  `json:"timestamp"`
	Signature    string `json:"signature"`
}

// Rebind rotates the agent's bound cert fingerprint on central. Auth is the
// proof-of-possession material in the body, not a bearer (see file comment).
func (c *CentralClient) Rebind(ctx context.Context, agentID string, req RebindRequest) (*AgentResponse, error) {
	var out AgentResponse
	path := "/api/v1/agents/" + agentID + "/rebind"
	if err := c.postJSON(ctx, path, req, &out); err != nil {
		return nil, fmt.Errorf("rebind certificate: %w", err)
	}
	return &out, nil
}

// Rebinder is the daemon-side trigger: debounced and single-flight so the
// renewal hook, the startup self-heal, and every loop's 401 callback can all
// fire it freely without stampeding central.
type Rebinder struct {
	Store   *CertStore
	Central *CentralClient
	Logger  *slog.Logger

	// MinInterval between attempts (default 1m). The auth-error callbacks fire
	// on every failed poll cycle, so the debounce — not the callers — is what
	// bounds the rebind rate.
	MinInterval time.Duration

	// Clock is injectable for tests (nil => time.Now).
	Clock func() time.Time

	mu       sync.Mutex
	last     time.Time
	inflight bool
}

func (r *Rebinder) clock() time.Time {
	if r.Clock != nil {
		return r.Clock()
	}
	return time.Now()
}

func (r *Rebinder) minInterval() time.Duration {
	if r.MinInterval > 0 {
		return r.MinInterval
	}
	return time.Minute
}

func (r *Rebinder) logger() *slog.Logger {
	if r.Logger != nil {
		return r.Logger
	}
	return slog.Default()
}

// Trigger attempts one rebind unless one ran recently or is running now.
// Safe to call from any goroutine; never blocks the caller beyond the attempt
// itself (callers who must not block wrap it in `go`). Failure is logged, not
// returned — every agent loop retries with backoff anyway, and the next 401
// re-triggers.
func (r *Rebinder) Trigger(ctx context.Context) {
	r.mu.Lock()
	now := r.clock()
	if r.inflight || now.Sub(r.last) < r.minInterval() {
		r.mu.Unlock()
		return
	}
	r.inflight = true
	r.last = now
	r.mu.Unlock()
	defer func() {
		r.mu.Lock()
		r.inflight = false
		r.mu.Unlock()
	}()

	if err := r.rebindOnce(ctx); err != nil {
		r.logger().Warn("certificate rebind failed (will retry on next trigger)", "err", err)
	}
}

func (r *Rebinder) rebindOnce(ctx context.Context) error {
	id, err := r.Store.Load()
	if err != nil {
		return fmt.Errorf("load identity: %w", err)
	}
	fp := CertFingerprint(id.Leaf)
	ts := r.clock().Unix()
	sig, err := SignRebind(id.Key, RebindPayload(id.State.AgentID, ts, fp))
	if err != nil {
		return err
	}
	chain := certToPEM(id.Leaf)
	for _, c := range id.Chain {
		chain = append(chain, certToPEM(c)...)
	}
	resp, err := r.Central.Rebind(ctx, id.State.AgentID, RebindRequest{
		CertChainPEM: string(chain),
		Timestamp:    ts,
		Signature:    sig,
	})
	if err != nil {
		return err
	}
	r.logger().Info("certificate fingerprint rebound with central",
		"agent_id", id.State.AgentID, "fingerprint", resp.CertFingerprint)
	return nil
}
