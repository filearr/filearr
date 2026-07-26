package enroll

import (
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/sha256"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"
)

// TestRebindPayloadGolden pins the exact canonical bytes. This is the
// cross-language vector: backend/tests/test_agent_rebind.py asserts the SAME
// bytes from agentcert.canonical_payload — if this test needs updating, the
// Python one does too (and the protocol version suffix should bump).
func TestRebindPayloadGolden(t *testing.T) {
	got := RebindPayload("agent-123", 1700000000, "abcdef0123")
	want := []byte("filearr-agent-rebind-v1\nagent-123\n1700000000\nabcdef0123")
	if !bytes.Equal(got, want) {
		t.Fatalf("canonical payload drifted:\n got %q\nwant %q", got, want)
	}
}

func TestSignRebindRoundtrip(t *testing.T) {
	key, _ := selfSigned(t, "roundtrip")
	payload := RebindPayload("a", 1234, "fp")
	sigB64, err := SignRebind(key, payload)
	if err != nil {
		t.Fatalf("sign: %v", err)
	}
	sig, err := base64.StdEncoding.DecodeString(sigB64)
	if err != nil {
		t.Fatalf("decode: %v", err)
	}
	digest := sha256.Sum256(payload)
	if !ecdsa.VerifyASN1(&key.PublicKey, digest[:], sig) {
		t.Fatalf("signature does not verify")
	}
	// A tampered payload must not verify.
	tampered := sha256.Sum256(RebindPayload("a", 1235, "fp"))
	if ecdsa.VerifyASN1(&key.PublicKey, tampered[:], sig) {
		t.Fatalf("signature verified a tampered payload")
	}
}

// newRebindServer returns an httptest central whose /rebind handler verifies
// the proof-of-possession material exactly as backend agentcert does (chain
// parse -> leaf fp -> signature over the canonical payload) and counts hits.
func newRebindServer(t *testing.T, agentID string, hits *atomic.Int32) *httptest.Server {
	t.Helper()
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/agents/"+agentID+"/rebind", func(w http.ResponseWriter, r *http.Request) {
		hits.Add(1)
		var req RebindRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, "bad json", http.StatusBadRequest)
			return
		}
		block, _ := pem.Decode([]byte(req.CertChainPEM))
		if block == nil {
			http.Error(w, "no pem", http.StatusBadRequest)
			return
		}
		leaf, err := x509.ParseCertificate(block.Bytes)
		if err != nil {
			http.Error(w, "bad leaf", http.StatusBadRequest)
			return
		}
		fp := CertFingerprint(leaf)
		sig, err := base64.StdEncoding.DecodeString(req.Signature)
		if err != nil {
			http.Error(w, "bad sig encoding", http.StatusBadRequest)
			return
		}
		digest := sha256.Sum256(RebindPayload(agentID, req.Timestamp, fp))
		pub, ok := leaf.PublicKey.(*ecdsa.PublicKey)
		if !ok || !ecdsa.VerifyASN1(pub, digest[:], sig) {
			http.Error(w, "signature does not verify", http.StatusUnauthorized)
			return
		}
		_ = json.NewEncoder(w).Encode(AgentResponse{ID: agentID, Status: "active", CertFingerprint: fp})
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return srv
}

// TestRebinderPostsVerifiableRequest: a Trigger loads the on-disk identity,
// signs the canonical payload with the leaf key, and posts material the server
// can independently verify against the presented chain.
func TestRebinderPostsVerifiableRequest(t *testing.T) {
	agentID := uuidV4()
	dir := t.TempDir()
	store := NewCertStore(dir)
	key, leaf := selfSigned(t, agentID)
	_, root := selfSigned(t, "root")
	if err := store.SaveIdentity(Identity{
		Key: key, Leaf: leaf, Roots: []*x509.Certificate{root},
		State: State{AgentID: agentID},
	}); err != nil {
		t.Fatalf("save: %v", err)
	}

	var hits atomic.Int32
	srv := newRebindServer(t, agentID, &hits)
	r := &Rebinder{Store: store, Central: NewCentralClient(srv.URL)}
	r.Trigger(context.Background())
	if hits.Load() != 1 {
		t.Fatalf("rebind endpoint hit %d times, want 1", hits.Load())
	}
}

// TestRebinderDebounce: repeated triggers inside MinInterval collapse to one
// request — the 401 callbacks fire every failed poll cycle, so the debounce is
// what protects central from a rebind stampede.
func TestRebinderDebounce(t *testing.T) {
	agentID := uuidV4()
	dir := t.TempDir()
	store := NewCertStore(dir)
	key, leaf := selfSigned(t, agentID)
	_, root := selfSigned(t, "root")
	if err := store.SaveIdentity(Identity{
		Key: key, Leaf: leaf, Roots: []*x509.Certificate{root},
		State: State{AgentID: agentID},
	}); err != nil {
		t.Fatalf("save: %v", err)
	}

	var hits atomic.Int32
	srv := newRebindServer(t, agentID, &hits)
	now := time.Unix(1700000000, 0)
	r := &Rebinder{
		Store:       store,
		Central:     NewCentralClient(srv.URL),
		MinInterval: time.Minute,
		Clock:       func() time.Time { return now },
	}
	for i := 0; i < 5; i++ {
		r.Trigger(context.Background())
	}
	if hits.Load() != 1 {
		t.Fatalf("debounce failed: %d requests, want 1", hits.Load())
	}
	// Past the interval the next trigger goes through.
	now = now.Add(2 * time.Minute)
	r.Trigger(context.Background())
	if hits.Load() != 2 {
		t.Fatalf("post-interval trigger did not fire: %d requests, want 2", hits.Load())
	}
}
