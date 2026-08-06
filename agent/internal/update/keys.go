package update

import (
	"crypto/ed25519"
	"encoding/base64"
	"fmt"
	"strings"
)

// PublicKeyBase64 is the release-signing public key material pinned into the
// agent binary at BUILD time. The default is EMPTY: a binary built without the
// pin cannot verify any manifest and therefore refuses every update
// (fail-closed). A real deployment overrides it at link time:
//
//	go build -ldflags "-X github.com/filearr/filearr/agent/internal/update.PublicKeyBase64=<base64-pubkey>" \
//	    ./cmd/filearr-agent
//
// The value may hold MULTIPLE comma-separated keys ("<current>,<next>") — the
// dual-pin rotation scheme (ops runbook §8.7): pin current+next, roll the fleet
// through the normal update channel, start signing with next, then drop the old
// key on the following build. A manifest verifying against ANY pinned key is
// accepted.
//
// Each key is what `filearr-release keygen` prints. The private key NEVER lives
// in the repo or on the central server — it stays on the operator's signing
// machine or hardware token (research §8: keeps "central compromised" from
// implying "attacker can push a malicious agent update").
var PublicKeyBase64 = ""

// PinnedKeys returns every decoded pinned public key, or ErrNoPinnedKey when
// the binary was built without a pin. Any single malformed entry fails the
// WHOLE set (never silently drop a key an operator thought was pinned).
func PinnedKeys() ([]ed25519.PublicKey, error) {
	s := strings.TrimSpace(PublicKeyBase64)
	if s == "" {
		return nil, ErrNoPinnedKey
	}
	var keys []ed25519.PublicKey
	for _, part := range strings.Split(s, ",") {
		k, err := DecodePublicKey(part)
		if err != nil {
			return nil, err
		}
		keys = append(keys, k)
	}
	return keys, nil
}

// DecodePublicKey decodes a base64 std-encoded Ed25519 public key. An empty or
// malformed value yields ErrNoPinnedKey / a wrapped decode error, never a
// zero-length key that would silently accept anything.
func DecodePublicKey(b64 string) (ed25519.PublicKey, error) {
	s := strings.TrimSpace(b64)
	if s == "" {
		return nil, ErrNoPinnedKey
	}
	raw, err := base64.StdEncoding.DecodeString(s)
	if err != nil {
		return nil, fmt.Errorf("update: decode pinned public key: %w", err)
	}
	if len(raw) != ed25519.PublicKeySize {
		return nil, fmt.Errorf("update: pinned public key wrong size: got %d, want %d", len(raw), ed25519.PublicKeySize)
	}
	return ed25519.PublicKey(raw), nil
}
