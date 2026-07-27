package main

import (
	"context"
	"os"
	"path/filepath"
	"strconv"

	agentcfg "github.com/filearr/filearr/agent/internal/config"
	"github.com/filearr/filearr/agent/internal/history"
	"github.com/filearr/filearr/agent/internal/index"
	"github.com/filearr/filearr/agent/internal/localapi"
	"github.com/filearr/filearr/agent/internal/query"
)

// envWebUIAddr overrides the local web UI loopback bind address (host:port). The
// -web-addr flag wins when both are set.
const envWebUIAddr = "FILEARR_AGENT_WEBUI_ADDR"

// envWebUIAllowRemote (containerized/NAS agents) opts the web UI into a
// NON-loopback bind: a Docker port mapping cannot reach a loopback listener,
// so a container exposing the UI needs this. With it set (and no explicit
// addr) the bind defaults to 0.0.0.0:8686. The central policy gate
// (web_ui_enabled) and the auth gate still apply — this only widens the
// LISTENER, deliberately and loudly (a startup WARN names the exposure).
const envWebUIAllowRemote = "FILEARR_AGENT_WEBUI_ALLOW_REMOTE"

// startWebUI launches the P7-T5 local web UI for the `run` daemon: a read-only
// browser search surface on a loopback TCP listener (127.0.0.1), SEPARATE from the
// P7-T2 unix-socket/named-pipe query transport (browsers can't dial a UDS). It
// returns a done-channel for a clean stop.
//
// The listener gates on the cached policy's EFFECTIVE web-UI capability
// (web_ui_enabled AND policy fresh within offline grace — config.LocalSurface /
// PolicyView.WebUIEnabled). A central disable, or the policy going stale past
// grace, takes the UI down within one gate interval with no central push (R4
// fail-closed asymmetry); the query socket transport is unaffected. A
// never-contacted agent starts with the web UI OFF.
func startWebUI(ctx context.Context, dataDir, webAddr string, idx *index.Store, hist *history.Store) <-chan struct{} {
	done := make(chan struct{})
	log := newLogger()

	searcher, err := query.NewSearcher(filepath.Join(dataDir, indexDBName))
	if err != nil {
		log.Error("local web UI disabled: cannot open read-only index", "err", err)
		close(done)
		return done
	}

	allowRemote, _ := strconv.ParseBool(os.Getenv(envWebUIAllowRemote))
	addr := webAddr
	if addr == "" {
		if allowRemote {
			addr = "0.0.0.0:8686" // container opt-in: reachable via port mapping
		} else {
			addr = localapi.DefaultWebAddr
		}
	}
	cache := agentcfg.NewETagCache(dataDir)

	wcfg := localapi.WebUIConfig{
		Addr:        addr,
		AllowRemote: allowRemote,
		Searcher:    searcher,
		Count:       func(ctx context.Context) (int, error) { return countActiveItems(ctx, idx) },
		Policy:      func() localapi.PolicyView { return loadPolicyView(cache) },
		Logger:      log,
	}
	// The web UI records history but is given only the write-side Recorder — it
	// cannot read history back (that surface is the socket API only).
	if hist != nil {
		wcfg.Recorder = hist
	}
	srv, err := localapi.NewWebUI(wcfg)
	if err != nil {
		log.Error("local web UI disabled: cannot initialize server", "err", err)
		searcher.Close()
		close(done)
		return done
	}

	go func() {
		defer close(done)
		defer searcher.Close()
		if err := srv.Run(ctx); err != nil && ctx.Err() == nil {
			log.Error("local web UI loop exited", "err", err)
		}
	}()
	return done
}
