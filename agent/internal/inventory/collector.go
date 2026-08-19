// Package inventory is the extensible inventory framework (W6-D3): a generic
// `inventory` agent command that walks an expanded set of roots (via the shared
// internal/pathspec engine + the W6-R1 presets) and runs a set of registered
// per-file COLLECTORS, emitting one NDJSON record per surviving entry plus a
// UI-facing summary. New inventory COMPOSITIONS (a preset + collector set) are
// authored centrally and need no agent redeployment — the vocabulary the agent
// advertises (Capabilities) is what an admin composes against.
//
// Collectors are metadata-only by contract: they stat/query attributes but NEVER
// open file CONTENT (the cloud-placeholder hydration hazard, W6-R1 §1.2/§3.3).
package inventory

import (
	"context"
	"encoding/json"
	"github.com/filearr/filearr/agent/internal/inventory/permissions"
	"io/fs"
	"os"
	"sort"

	"github.com/filearr/filearr/agent/internal/extract"
)

// CapabilityVersion is the inventory contract version the agent advertises. Bump
// it when the collector output shape or command payload contract changes in a way
// central must gate on.
const CapabilityVersion = 1

// Collector computes a set of metadata fields for one filesystem entry. Implementations
// MUST be metadata-only (no content reads). A returned error is per-file, per-collector
// and fail-soft: the runner records it and continues with the other collectors/entries.
type Collector interface {
	// Name is the stable identifier an admin composes against (matches a central
	// InventoryConfig.collectors entry).
	Name() string
	// Collect returns this collector's fields for path (info already stat'd by the
	// walk — reuse it, do not re-stat). ctx is honored for cancellation.
	Collect(ctx context.Context, path string, info fs.FileInfo) (map[string]any, error)
}

// Registry is an ordered, name-keyed set of collectors. It is the extension point:
// a future collector is Register()ed here (and, by capability advertisement,
// becomes composable centrally) with no other wiring.
type Registry struct {
	m     map[string]Collector
	order []string
}

// NewRegistry returns an empty registry.
func NewRegistry() *Registry {
	return &Registry{m: map[string]Collector{}}
}

// Register adds (or replaces) a collector, preserving first-registration order for
// a stable Names()/capability advertisement. Returns the registry for chaining.
func (r *Registry) Register(c Collector) *Registry {
	name := c.Name()
	if _, exists := r.m[name]; !exists {
		r.order = append(r.order, name)
	}
	r.m[name] = c
	return r
}

// Get resolves a collector by name.
func (r *Registry) Get(name string) (Collector, bool) {
	c, ok := r.m[name]
	return c, ok
}

// Names lists the registered collector names in registration order.
func (r *Registry) Names() []string {
	out := make([]string, len(r.order))
	copy(out, r.order)
	return out
}

// DefaultRegistry is the v1 built-in collector set: stat, owner, perms,
// placeholder. It is what the agent advertises and what a command's `collectors`
// list resolves against.
func DefaultRegistry() *Registry {
	r := NewRegistry().
		Register(statCollector{}).
		Register(ownerCollector{}).
		Register(permsCollector{}).
		Register(placeholderCollector{})
	// W7 (2026-08-19): the full-ACE permissions collector, advertised only on
	// platforms with a real read (Linux, Windows) so central never offers it
	// where every entry would be a per-file scaffold error.
	if permissions.Supported() {
		r = r.Register(permissions.Collector{})
	}
	return r
}

// Capabilities is the additive advertisement the agent attaches to its command
// poll so central can store what this agent supports (and the UI can offer only
// composable collectors). Shape: {inventory_collectors: [...], inventory_version: N,
// ffmpeg: bool, container: bool, extract: bool, extract_schema: N,
// tools: {...}, formats: [...]}. “ffmpeg“ (roadmap §20) lets the fleet console
// show which agents can produce video poster-frames — a missing binary used to be
// silently absent thumbs with no operator-visible signal.
//
// The extract/tools/formats block is the agent-parity design contract's runtime
// capability advertisement (2026-08-09). It is the whole reason there is ONE
// agent binary instead of an essentials/full split: the heavy capabilities are
// host tools, so a build degrades PER CAPABILITY at runtime and central's console
// can tell an operator exactly which policy keys this agent will ignore, and why.
// “ffmpeg“ stays a top-level key for the older centrals that read it there.
//
// 2026-08-11 adds the per-agent ABOUT payload — `tool_paths`, `build` and
// (budget permitting) `modules`. Two notes on that:
//
//   - `inventory_version` is NOT bumped. It versions the collector vocabulary a
//     central composes against, not this advertisement. Every key here is
//     additive and an older central ignores what it does not read; bumping the
//     contract version for an additive field would make centrals gate on a
//     capability change that never happened.
//   - This function is called ONCE PER POLL (it used to be called once per
//     process, at daemon start). That is what makes the TTL'd tool caches
//     visible: a tool installed after the agent started now appears in the
//     console within one toolCacheTTL, with no service restart.
func Capabilities() map[string]any {
	names := DefaultRegistry().Names()
	sorted := make([]string, len(names))
	copy(sorted, names)
	sort.Strings(sorted)
	caps := map[string]any{
		"inventory_collectors": sorted,
		"inventory_version":    CapabilityVersion,
		"ffmpeg":               HasFFmpeg(),
		// Containerized agents update by image pull — central flags a newer
		// build in the console but never offers them the self-update channel.
		"container": InContainer(),
		// This build carries the extraction pass; extract_schema is the
		// `extracted.schema` version its events will stamp.
		"extract":        true,
		"extract_schema": extract.Schema,
		"tools":          Tools(),
		// The VERSIONS of the tools that are present (2026-08-10). "ffmpeg: true"
		// answers whether a capability runs here; it does not answer which build
		// is running, which is the question an operator hits when OCR quality or
		// a codec probe looks wrong. Present-but-silent tools are omitted, so the
		// console renders "installed, version unknown" from the two maps rather
		// than an empty string.
		"tool_versions": ToolVersions(),
		// WHERE each present tool resolved from (2026-08-11, user request:
		// "pull the tool locations also"). Absent tools omitted. See
		// ToolPaths — the path is both the answer to "which copy is running"
		// and the visible proof of the no-user-directories security rule.
		"tool_paths": ToolPaths(),
		// This agent's own build provenance: toolchain, target platform, VCS
		// commit, host OS version. The agent-side half of central's About page,
		// which can interrogate its own process but has never had any way to
		// ask an agent the same questions.
		"build":   buildBlock(),
		"formats": ExtractFormats(),
	}
	attachModules(caps)
	return caps
}

// capabilitiesBudget is the size this advertisement keeps itself under, in
// bytes of marshalled JSON.
//
// Central caps a `capabilities` body at “agent_capabilities_max_bytes“
// (16 KiB, config.py) and DROPS an oversize one — the poll still succeeds, the
// commands still drain, and the stored advertisement is silently left at
// whatever it was before. That is a genuinely nasty failure mode from this
// side: adding a field could make an agent's entire capability report stop
// updating, with no error anywhere.
//
// So the agent measures itself and trims first. 12 KiB rather than 16 leaves
// headroom for two things: fields added later without this calculation being
// revisited, and the fact that central applies its cap to the body it decodes,
// not to the bytes we measured (key ordering and escaping can differ). The one
// block big enough to matter is `modules`, so that is the block that gets
// dropped — and dropped LOUDLY, via `modules_omitted`, because a console
// showing an empty dependency table would be asserting this binary has no
// dependencies.
//
// A var, not a const, so a test can shrink it without building a synthetic
// 12 KiB module graph.
var capabilitiesBudget = 12 * 1024

// moduleLister is Modules, indirected for tests that need an oversize graph.
var moduleLister = Modules

// attachModules adds the Go module dependency list to caps IF the result still
// fits the budget, and otherwise records that it was left out.
//
// Measured by actually marshalling, not estimated: the estimate is what would
// be wrong, and it would be wrong silently in exactly the case that matters.
func attachModules(caps map[string]any) {
	mods := moduleLister()
	if len(mods) == 0 {
		// No build table to read (see Modules). Neither a list nor an omission
		// flag — there is nothing to report and nothing was suppressed.
		return
	}
	caps["modules"] = mods
	if b, err := json.Marshal(caps); err == nil && len(b) <= capabilitiesBudget {
		return
	}
	delete(caps, "modules")
	// An explicit flag rather than a bare absence: the console says "not
	// reported (payload budget)" and offers the agent's local web UI, which has
	// no such limit because nothing crosses a network to reach it.
	caps["modules_omitted"] = true
}

// buildBlock is BuildInfo plus the host OS version — build-time facts and the
// one run-time platform fact that belongs with them.
func buildBlock() map[string]any {
	b := BuildInfo()
	if v := OSVersion(); v != "" {
		b["os_version"] = v
	}
	return b
}

// InContainer reports whether the agent runs inside a container. The shipped
// agent image sets FILEARR_AGENT_CONTAINER=1 explicitly ("0"/"false" opt back
// out for someone who genuinely wants in-container self-update swaps);
// /.dockerenv catches hand-rolled containers.
func InContainer() bool {
	if v := os.Getenv("FILEARR_AGENT_CONTAINER"); v != "" {
		return v != "0" && v != "false"
	}
	if _, err := os.Stat("/.dockerenv"); err == nil {
		return true
	}
	return false
}
