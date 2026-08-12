package inventory

import (
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"sync"
	"time"

	"github.com/filearr/filearr/agent/internal/extract"
)

// Host-tool path overrides. Each mirrors the long-standing
// FILEARR_AGENT_FFMPEG_PATH convention the thumbnailer established: when set the
// variable NAMES the binary to use (an absolute path, or a bare name resolved on
// PATH); when unset the tool's conventional name is looked up on PATH. This is
// the whole "capability = host tool" story from the agent-parity design — an
// operator upgrades an agent's capabilities by installing a binary, never by
// swapping the agent build.
const (
	EnvFFmpegPath    = "FILEARR_AGENT_FFMPEG_PATH"
	EnvFFprobePath   = "FILEARR_AGENT_FFPROBE_PATH"
	EnvTesseractPath = "FILEARR_AGENT_TESSERACT_PATH"
	EnvExiftoolPath  = "FILEARR_AGENT_EXIFTOOL_PATH"
	// The three poppler-utils binaries the PDF story rides on. They are detected
	// SEPARATELY rather than as one "poppler" capability because a host can ship
	// a partial install (Windows zip drops, minimal distro packages), and the
	// honest answer to "will PDF text work here" is per binary.
	EnvPDFInfoPath   = "FILEARR_AGENT_PDFINFO_PATH"
	EnvPDFToTextPath = "FILEARR_AGENT_PDFTOTEXT_PATH"
	EnvPDFToPPMPath  = "FILEARR_AGENT_PDFTOPPM_PATH"
)

// toolKey memoizes a lookup by BOTH the tool name and the override value in
// force. A PATH lookup costs a handful of stat calls and the extraction pass
// asks per file, so the answer is memoized; keying on the override value keeps
// the cache honest, because a test — or a service restarted with a different
// environment — that changes the variable gets a fresh lookup instead of a
// stale hit. There is no cache-invalidation hook to forget to call.
type toolKey struct{ name, override string }

// cachedString is a memoized answer plus WHEN it was learned. The timestamp is
// the whole point: see toolCacheTTL.
type cachedString struct {
	value string
	at    time.Time
}

// toolCacheTTL bounds how long a resolution (and the version probe keyed off
// it) is reused before the tool is looked for again.
//
// WHY a TTL at all — this cache used to be process-lifetime, and that turned a
// two-minute fix into a support case (live, 2026-08-11). An operator installed
// exiftool machine-wide on a Windows agent host, watched the console keep
// reporting it absent, and had no way to know that the only cure was restarting
// the service: Capabilities() was computed once at daemon start and every tool
// answer inside it was frozen for the life of the process. An agent that cannot
// notice a tool being installed is an agent whose capability report is a
// statement about boot time, not about the host.
//
// The cost of the TTL is bounded and small: at most one LookPath plus one
// 3 s-bounded version probe per tool per 15 minutes (seven tools => seven
// subprocesses per quarter hour, against a command poll that runs every 60 s).
// The extraction hot path, which asks per FILE, still gets a cache hit
// essentially always.
//
// A package var rather than a const so tests can collapse it to zero and prove
// a stale entry re-probes, without sleeping for a quarter of an hour.
var toolCacheTTL = 15 * time.Minute

// cacheClock is the time source for cache entries; a var so a test can freeze
// or advance it. Everything else in this package calls time.Now directly.
var cacheClock = time.Now

var toolCache sync.Map // toolKey -> cachedString ("" value == not resolvable)

// cacheLoad returns a memoized answer that is still within toolCacheTTL. A
// stale entry reports a miss (and is simply overwritten by the re-probe) rather
// than being deleted: concurrent readers of a stale entry re-probing in
// parallel is harmless, and Delete+Store would open a window where a caller
// sees no entry at all.
func cacheLoad(m *sync.Map, key toolKey) (string, bool) {
	v, ok := m.Load(key)
	if !ok {
		return "", false
	}
	entry, ok := v.(cachedString)
	if !ok || cacheClock().Sub(entry.at) >= toolCacheTTL {
		return "", false
	}
	return entry.value, true
}

func cacheStore(m *sync.Map, key toolKey, value string) {
	m.Store(key, cachedString{value: value, at: cacheClock()})
}

// ResolveTool returns the resolvable path of the host tool `name`, honoring its
// env override, or "" when the tool is absent. Never fatal: an absent tool is a
// capability the agent simply does not advertise.
func ResolveTool(name, envVar string) string {
	override := os.Getenv(envVar)
	key := toolKey{name: name, override: override}
	if v, ok := cacheLoad(&toolCache, key); ok {
		return v
	}
	want := name
	if override != "" {
		want = override
	}
	// LookPath on an absolute path still checks existence + executability, which
	// is exactly the semantics the original HasFFmpeg had for its override.
	resolved, err := exec.LookPath(want)
	if err != nil {
		resolved = ""
	}
	// PATH miss, no explicit override: try the places these tools actually get
	// installed. This is not a convenience — it is a correctness fix for the
	// SERVICE case, which is how the agent normally runs.
	//
	// A Windows service, a launchd job and a systemd unit each start with a
	// PATH that has nothing to do with the operator's interactive shell. The
	// Tesseract installer does not add itself to PATH at all (live report,
	// 2026-08-10: installed at C:\Program Files\Tesseract-OCR on a Windows agent
	// and reported as absent), and Homebrew's /opt/homebrew/bin is famously
	// missing from a launchd job's default PATH. In every one of those cases the
	// operator has genuinely installed the tool, the console says "not
	// installed", and the only fix was an env var they had no reason to expect
	// they needed.
	//
	// The override still wins and is still the documented escape hatch for a
	// non-standard location; this only runs when PATH found nothing and the
	// operator said nothing.
	if resolved == "" && override == "" {
		resolved = searchWellKnown(name)
	}
	cacheStore(&toolCache, key, resolved)
	return resolved
}

// searchWellKnown probes the conventional install locations for `name` on this
// OS, returning the first executable hit or "". Each candidate is verified with
// LookPath, so a stale directory or a non-executable file is skipped rather than
// returned as a path that later fails to run.
func searchWellKnown(name string) string {
	for _, dir := range wellKnownDirs(name) {
		if dir == "" {
			continue
		}
		candidate := filepath.Join(dir, name+exeSuffix)
		if p, err := exec.LookPath(candidate); err == nil {
			return p
		}
	}
	return ""
}

// FFmpegPath resolves the host ffmpeg (video poster frames), or "".
func FFmpegPath() string { return ResolveTool("ffmpeg", EnvFFmpegPath) }

// FFprobePath resolves the host ffprobe (media technical probe), or "".
func FFprobePath() string { return ResolveTool("ffprobe", EnvFFprobePath) }

// TesseractPath resolves the host tesseract (OCR), or "". OCR cannot be pure-Go
// at any quality, so this tool IS the capability.
func TesseractPath() string { return ResolveTool("tesseract", EnvTesseractPath) }

// ExiftoolPath resolves the host exiftool (deep EXIF), or "". Central invokes
// the same binary per file (never in-process linking — the exiv2 GPL-linking
// ambiguity), so this is exact parity, not an approximation of it.
func ExiftoolPath() string { return ResolveTool("exiftool", EnvExiftoolPath) }

// PDFInfoPath resolves poppler's pdfinfo (PDF page count + document properties).
func PDFInfoPath() string { return ResolveTool("pdfinfo", EnvPDFInfoPath) }

// PDFToTextPath resolves poppler's pdftotext (PDF body text).
func PDFToTextPath() string { return ResolveTool("pdftotext", EnvPDFToTextPath) }

// PDFToPPMPath resolves poppler's pdftoppm (page rasterisation for scanned-PDF
// OCR — the same binary central's ocr.py drives).
func PDFToPPMPath() string { return ResolveTool("pdftoppm", EnvPDFToPPMPath) }

// HasFFmpeg reports whether an ffmpeg binary is resolvable. Shared by the
// capability advertisement and the install-time requirements check.
func HasFFmpeg() bool { return FFmpegPath() != "" }

// HasFFprobe reports whether an ffprobe binary is resolvable.
func HasFFprobe() bool { return FFprobePath() != "" }

// HasTesseract reports whether a tesseract binary is resolvable.
func HasTesseract() bool { return TesseractPath() != "" }

// HasExiftool reports whether an exiftool binary is resolvable.
func HasExiftool() bool { return ExiftoolPath() != "" }

// HasPDFInfo / HasPDFToText / HasPDFToPPM report poppler-utils presence.
func HasPDFInfo() bool   { return PDFInfoPath() != "" }
func HasPDFToText() bool { return PDFToTextPath() != "" }
func HasPDFToPPM() bool  { return PDFToPPMPath() != "" }

// hostToolEnvs is THE list of host tools this agent knows about, mapped to the
// environment variable that overrides each one's location. It is deliberately
// the single source for the three views built from it below (presence,
// versions, paths): three separate literals is how one of them ends up missing
// a tool that the other two report, and the console then shows a matrix with a
// hole in it that nobody can explain.
var hostToolEnvs = map[string]string{
	"ffmpeg":    EnvFFmpegPath,
	"ffprobe":   EnvFFprobePath,
	"tesseract": EnvTesseractPath,
	"exiftool":  EnvExiftoolPath,
	"pdfinfo":   EnvPDFInfoPath,
	"pdftotext": EnvPDFToTextPath,
	"pdftoppm":  EnvPDFToPPMPath,
}

// HostToolNames lists the known host tools in a stable, sorted order. Used by
// the local web UI so its rows do not shuffle between refreshes (Go map
// iteration is deliberately randomized).
func HostToolNames() []string {
	names := make([]string, 0, len(hostToolEnvs))
	for name := range hostToolEnvs {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}

// Tools is the host-tool matrix the agent advertises (and the local status page
// renders). Keys are stable identifiers central's console gates on; a false
// value means "this host cannot do the capability that tool backs".
func Tools() map[string]bool {
	out := make(map[string]bool, len(hostToolEnvs))
	for name, env := range hostToolEnvs {
		out[name] = ResolveTool(name, env) != ""
	}
	return out
}

// ToolPaths is the LOCATION half of the host-tool matrix: the resolved absolute
// path of every tool that is present, absent tools omitted (the same
// omit-don't-blank discipline ToolVersions uses, so the console renders "not
// installed" from Tools() rather than an empty string that reads as a bug).
//
// WHY central is told the path (user request, 2026-08-11: "pull the tool
// locations also"). "exiftool: yes" does not say WHICH exiftool, and on a
// Windows host there are routinely several — a five-year-old zip in
// C:\Program Files, a Chocolatey shim, a winget portable payload. When a
// version looks wrong or output looks wrong, the path is the fact that ends the
// argument.
//
// It is also the visible proof of a SECURITY property. Since 2026-08-11 the
// resolver only ever searches machine-wide, admin-writable locations, because
// this agent normally runs as LocalSystem/root and executing a binary out of a
// user-writable directory would let any local user escalate. A displayed path
// under a user profile would therefore mean that rule has regressed — the
// negative is pinned by TestWellKnownNeverProbesUserProfile, and this display
// is the second line of defence that an operator can see.
//
// Reuses ResolveTool, deliberately: a second resolution path here is exactly
// how the displayed location and the executed location would drift apart, and
// the displayed one would be the lie.
func ToolPaths() map[string]string {
	out := map[string]string{}
	for name, env := range hostToolEnvs {
		if p := ResolveTool(name, env); p != "" {
			out[name] = p
		}
	}
	return out
}

// ExtractFormats lists the taxonomy file_category values this build can actually
// extract ON THIS HOST — compiled-in support intersected with detected tools.
// It is the honest answer to "what will extraction produce here", which is what
// the console shows next to the policy editor.
func ExtractFormats() []string { return extract.Formats(HasFFprobe()) }
