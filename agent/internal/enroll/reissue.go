package enroll

// Reissue: obtain a FRESH leaf certificate for an ALREADY ENROLLED agent from
// an operator-minted recovery OTT (central: POST /agents/{id}/ca-ott, admin —
// runbook §7.3). This is the missing client half of the documented expired-
// leaf recovery: step-ca refuses mTLS renewal of an expired cert, so an agent
// offline past its cert TTL was previously unrecoverable without a full
// re-enroll — which mints a NEW agent id and orphans the old identity,
// library link, and replication watermark (live 2026-08-05: a Windows agent
// expired while its service was broken). Reissue keeps the on-disk State
// (agent id, central URL, CA pin) verbatim; only key + certs rotate, and the
// existing rebind flow re-binds the new fingerprint with central.

import (
	"context"
	"crypto/x509"
	"fmt"
	"log/slog"

	"github.com/smallstep/certificates/ca"
)

// Reissuer swaps in a fresh leaf via a §7.3 recovery OTT.
type Reissuer struct {
	Store  *CertStore
	OTT    string
	Logger *slog.Logger

	// caFactory is overridable for tests; nil => defaultCAClientFactory.
	caFactory caClientFactory
}

// Reissue obtains + persists the new identity and re-binds its fingerprint
// with central. Returns the refreshed identity.
func (r *Reissuer) Reissue(ctx context.Context) (*Identity, error) {
	st, err := r.Store.LoadState()
	if err != nil {
		return nil, fmt.Errorf("reissue needs an enrolled identity (state.json): %w", err)
	}

	factory := r.caFactory
	if factory == nil {
		factory = defaultCAClientFactory
	}
	caClient, err := factory(st.CAURL, st.CARootSHA256)
	if err != nil {
		return nil, err
	}

	// Same CSR-from-OTT construction as enroll: CN/SANs come from the OTT's
	// own claims (central mints sub == sans == agent_id), a fresh P-256 key is
	// generated. The SAN identity is re-checked below against OUR agent id so
	// an OTT minted for a different agent can never clobber this identity.
	signReq, key, err := ca.CreateSignRequest(r.OTT)
	if err != nil {
		return nil, fmt.Errorf("create sign request from ott: %w", err)
	}
	signResp, err := caClient.Sign(signReq)
	if err != nil {
		return nil, fmt.Errorf("step-ca sign: %w", err)
	}
	leaf, chain, err := certsFromSignResponse(signResp)
	if err != nil {
		return nil, err
	}
	if len(leaf.DNSNames) == 0 || leaf.DNSNames[0] != st.AgentID {
		return nil, fmt.Errorf(
			"reissued certificate is for %q, not this agent (%s) — the OTT was minted for a different agent",
			firstDNS(leaf), st.AgentID)
	}

	rootsResp, err := caClient.Roots()
	if err != nil {
		return nil, fmt.Errorf("fetch CA roots: %w", err)
	}
	roots := make([]*x509.Certificate, 0, len(rootsResp.Certificates))
	for _, c := range rootsResp.Certificates {
		roots = append(roots, c.Certificate)
	}

	// Persist with the EXISTING state — identity continuity is the point.
	if err := r.Store.SaveIdentity(Identity{
		Key: key, Leaf: leaf, Chain: chain, Roots: roots, State: st,
	}); err != nil {
		return nil, fmt.Errorf("persist reissued identity: %w", err)
	}

	// Re-bind the new fingerprint with central via the standard signed-payload
	// rebind (valid fresh leaf => passes the validity-window check that the
	// expired one could not). Non-fatal: the daemon's startup/401 rebind
	// triggers self-heal if central is briefly unreachable right now.
	reb := &Rebinder{Store: r.Store, Central: NewCentralClient(st.CentralURL), Logger: r.Logger}
	if err := reb.rebindOnce(ctx); err != nil {
		r.log().Warn("fingerprint rebind after reissue failed (the daemon will retry)", "err", err)
	}

	id, err := r.Store.Load()
	if err != nil {
		return nil, err
	}
	return id, nil
}

func (r *Reissuer) log() *slog.Logger {
	if r.Logger != nil {
		return r.Logger
	}
	return slog.Default()
}

func firstDNS(c *x509.Certificate) string {
	if len(c.DNSNames) > 0 {
		return c.DNSNames[0]
	}
	return c.Subject.CommonName
}
