package main

// Share-map layering (2026-08-10). Two surfaces can now supply the static
// network-share locations the agent attaches to items as best-effort hints
// (P10-T11, Architect ruling R1 — a missing hint is normal, never an error):
//
//   - FILEARR_AGENT_SHARE_MAP, the machine's environment, written by whoever
//     deploys the container/service (a compose file, an Unraid template);
//   - share_mappings in local-settings.json, written from the agent's own web UI
//     by an operator at the machine, when central granted local_roots_control.
//
// This file owns the precedence between them, and the display view both the
// Controls tab and the Status panel render.

import (
	"os"
	"sort"
	"sync"

	agentcfg "github.com/filearr/filearr/agent/internal/config"
	"github.com/filearr/filearr/agent/internal/localapi"
	"github.com/filearr/filearr/agent/internal/shares"
)

// localShareSource is the provenance label for a mapping authored in the local
// web UI, matching the wording the schedule controls already use for
// local-settings.json ("local override").
const localShareSource = "local override"

// staticShareMappings assembles the static share map in PRECEDENCE ORDER — the
// resolver takes the first mapping for a given local path — together with every
// malformed entry, from either surface, kept verbatim for display.
//
// # Precedence: the environment wins over a locally-authored mapping
//
// The documented chain for the SCHEDULE knobs is
//
//	central policy > local override > FILEARR_AGENT_* env > sidecar > default
//
// (docs-site/reference/agent-settings.md). The share map deliberately sits the
// other way round for the env layer, and the reason is the one that put local
// UNDER central there: never accept an edit whose value is not the value the
// system will use, and never let one layer silently contradict another.
//
//   - Share hints have NO central key at all — the reference calls them
//     host-shaped settings central has no business knowing — so the top of this
//     chain is the host's own configuration, which is the environment.
//   - FILEARR_AGENT_SHARE_MAP is what the deployment manifest (compose file,
//     Unraid template) declares. An operator reading that manifest must be able
//     to trust that it describes what this agent reports; a local edit that
//     silently outranked it would make the manifest a lie with nothing in the
//     manifest to show for it. The web UI cannot edit the manifest, so the only
//     honest options are "local wins silently" or "the env-declared path is
//     locked and says so" — and this codebase already chose the latter shape for
//     centrally-set keys (webcontrol.go rule 2).
//   - Nothing is silently reverted by this order, unlike the central case: the
//     local edit endpoint REFUSES a path the environment already maps (409,
//     naming the variable), and the UI renders such a row read-only and
//     env-provided. Local mappings fill in the paths the environment leaves
//     unmapped, exactly as local schedule overrides fill in the keys central
//     left unset.
func staticShareMappings(dataDir string, getenv func(string) string) ([]shares.Mapping, []shares.Reject) {
	envMappings, rejects := shares.ParseSpec(getenv(envShareMap), shares.StaticMapSource)
	local, _ := agentcfg.LoadLocalSettings(dataDir) // corrupt => no local mappings, never fatal
	localMappings, localRejects := localShareMappings(local)
	rejects = append(rejects, localRejects...)
	// Env first: SetStaticMappings keeps the FIRST mapping for a local path.
	return append(envMappings, localMappings...), rejects
}

// localShareMappings turns the local override document's share_mappings into
// resolver mappings, in a stable (sorted) order so two renders of the same file
// never disagree. A hand-edited malformed value is rejected here rather than
// installed: the resolver would skip it anyway (R1), and a reject the UI can
// show is strictly better than a mapping that quietly does nothing.
func localShareMappings(ls agentcfg.LocalSettings) ([]shares.Mapping, []shares.Reject) {
	paths := make([]string, 0, len(ls.ShareMappings))
	for p := range ls.ShareMappings {
		paths = append(paths, p)
	}
	sort.Strings(paths)
	var out []shares.Mapping
	var bad []shares.Reject
	for _, p := range paths {
		loc := ls.ShareMappings[p]
		if shares.ValidateLocation(loc) != nil {
			bad = append(bad, shares.Reject{Entry: p + "=" + loc, Source: localShareSource})
			continue
		}
		out = append(out, shares.Mapping{Local: p, Location: loc, Source: localShareSource})
	}
	return out, bad
}

// envShareMapPaths lists the local paths the ENVIRONMENT maps. These are the
// paths locked against local editing (see staticShareMappings), so the local API
// needs them to refuse an edit before it is stored rather than after it is
// ignored.
func envShareMapPaths(getenv func(string) string) []string {
	mappings, _ := shares.ParseSpec(getenv(envShareMap), shares.StaticMapSource)
	out := make([]string, 0, len(mappings))
	for _, m := range mappings {
		out = append(out, m.Local)
	}
	return out
}

// shareResolverFor builds a resolver carrying both layers. The scan process and
// the daemon's web UI both call it, so the location the UI shows for a root is
// resolved by the same code, from the same inputs, as the hint a scan attaches
// to a file under that root.
func shareResolverFor(dataDir string, getenv func(string) string) (*shares.Resolver, []shares.Reject) {
	r := shares.New(getenv(envShareHost))
	mappings, rejects := staticShareMappings(dataDir, getenv)
	if len(mappings) > 0 {
		r.SetStaticMappings(mappings)
	}
	return r, rejects
}

// displayResolver is the web UI's resolver, kept for the process lifetime.
// Enumeration shells out (a PowerShell call on Windows), and its result is
// cached inside the resolver for a TTL — so building a NEW resolver per request
// would re-enumerate the host on every page load. The static mappings are
// re-installed on each call instead, which is free and picks up an edit the
// operator just made without disturbing that cache.
var (
	displayResolverMu sync.Mutex
	displayResolver   *shares.Resolver
)

func cachedDisplayResolver(dataDir string, getenv func(string) string) (*shares.Resolver, []shares.Reject) {
	mappings, rejects := staticShareMappings(dataDir, getenv)
	displayResolverMu.Lock()
	defer displayResolverMu.Unlock()
	if displayResolver == nil {
		displayResolver = shares.New(getenv(envShareHost))
	}
	displayResolver.SetStaticMappings(mappings)
	return displayResolver, rejects
}

// rootShareViews resolves each configured scan root to the network location a
// file under it would report, with the surface that supplied it. Roots with no
// covering mapping come back with an empty Location — the explicit "no share
// mapping" state, which is the useful signal: such a root produces no share hint
// at all, and nothing else on the agent says so.
//
// Discovery is included (a binary agent on a real SMB host may need no map at
// all); enumeration is cached inside the resolver, so one call per render costs
// at most one enumeration per TTL.
func rootShareViews(dataDir string, roots []string, getenv func(string) string) ([]localapi.RootShare, []localapi.ShareMapReject) {
	resolver, rejects := cachedDisplayResolver(dataDir, getenv)
	local, _ := agentcfg.LoadLocalSettings(dataDir)
	envMappings, _ := shares.ParseSpec(getenv(envShareMap), shares.StaticMapSource)

	envFor := func(path string) string {
		for _, m := range envMappings {
			if shares.SamePath(m.Local, path) {
				return m.Location
			}
		}
		return ""
	}
	localFor := func(path string) string {
		for p, loc := range local.ShareMappings {
			if shares.SamePath(p, path) {
				return loc
			}
		}
		return ""
	}

	views := make([]localapi.RootShare, 0, len(roots))
	for _, root := range roots {
		res := resolver.Resolve(root)
		v := localapi.RootShare{
			Path:       root,
			Source:     res.Source,
			EnvValue:   envFor(root),
			LocalValue: localFor(root),
			Ambiguous:  res.Ambiguous,
		}
		if res.Hint != nil {
			v.Location = res.Hint.ShareURL
			v.UNC = res.Hint.UNC
			if !shares.SamePath(res.ExportPath, root) {
				// The mapping covers a PARENT of this root. Say so: an operator
				// editing "the mapping for this root" must know the value lives
				// on another path and covers more than what they are looking at.
				v.InheritedFrom = res.ExportPath
			}
		}
		if v.EnvValue != "" && v.LocalValue != "" {
			// Only reachable when the environment gained an entry after a local
			// one existed (the edit endpoint refuses the reverse order). Report
			// it rather than let the stored-but-unused value look effective.
			v.Superseded = true
		}
		views = append(views, v)
	}

	out := make([]localapi.ShareMapReject, 0, len(rejects))
	for _, rj := range rejects {
		out = append(out, localapi.ShareMapReject{Entry: rj.Entry, Source: rj.Source})
	}
	return views, out
}

// setLocalShareMapping stores (or clears, on an empty location) one locally
// authored mapping. Validation happened in the API layer with the resolver's own
// parser; this is the persistence half.
func setLocalShareMapping(dataDir, path, location string) error {
	_, err := agentcfg.UpdateLocalSettings(dataDir, func(ls *agentcfg.LocalSettings) {
		if location == "" {
			deleteShareMapping(ls, path)
			return
		}
		// Replace any equivalent-path key rather than adding a second spelling
		// of the same directory (C:\Media vs c:/media on Windows) — two keys
		// denoting one path would make the stored document ambiguous.
		deleteShareMapping(ls, path)
		if ls.ShareMappings == nil {
			ls.ShareMappings = map[string]string{}
		}
		ls.ShareMappings[path] = location
	})
	return err
}

// deleteShareMapping removes every key denoting path (platform path rules, not
// string equality).
func deleteShareMapping(ls *agentcfg.LocalSettings, path string) {
	for p := range ls.ShareMappings {
		if shares.SamePath(p, path) {
			delete(ls.ShareMappings, p)
		}
	}
	if len(ls.ShareMappings) == 0 {
		ls.ShareMappings = nil // keep the document clean rather than storing {}
	}
}

// osGetenv is the process environment, named so the share-map helpers can be
// unit-tested with a stub without reaching for t.Setenv on shared state.
func osGetenv(key string) string { return os.Getenv(key) }
