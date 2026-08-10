package inventory

import (
	"os"
	"os/exec"
	"sync"

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
)

// toolKey memoizes a lookup by BOTH the tool name and the override value in
// force. Detection is process-lifetime stable (a PATH lookup costs a handful of
// stat calls, but the extraction pass asks per file), yet keying on the override
// value keeps the cache honest: a test — or a service restarted with a different
// environment — that changes the variable gets a fresh lookup instead of a stale
// hit. There is no cache-invalidation hook to forget to call.
type toolKey struct{ name, override string }

var toolCache sync.Map // toolKey -> string ("" == not resolvable)

// ResolveTool returns the resolvable path of the host tool `name`, honoring its
// env override, or "" when the tool is absent. Never fatal: an absent tool is a
// capability the agent simply does not advertise.
func ResolveTool(name, envVar string) string {
	override := os.Getenv(envVar)
	key := toolKey{name: name, override: override}
	if v, ok := toolCache.Load(key); ok {
		return v.(string)
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
	toolCache.Store(key, resolved)
	return resolved
}

// FFmpegPath resolves the host ffmpeg (video poster frames), or "".
func FFmpegPath() string { return ResolveTool("ffmpeg", EnvFFmpegPath) }

// FFprobePath resolves the host ffprobe (media technical probe), or "".
func FFprobePath() string { return ResolveTool("ffprobe", EnvFFprobePath) }

// TesseractPath resolves the host tesseract (OCR), or "". OCR cannot be pure-Go
// at any quality, so this tool IS the capability.
func TesseractPath() string { return ResolveTool("tesseract", EnvTesseractPath) }

// ExiftoolPath resolves the host exiftool (deep EXIF), or "". Detected and
// advertised now; the EXIF extractor itself is a later phase.
func ExiftoolPath() string { return ResolveTool("exiftool", EnvExiftoolPath) }

// HasFFmpeg reports whether an ffmpeg binary is resolvable. Shared by the
// capability advertisement and the install-time requirements check.
func HasFFmpeg() bool { return FFmpegPath() != "" }

// HasFFprobe reports whether an ffprobe binary is resolvable.
func HasFFprobe() bool { return FFprobePath() != "" }

// HasTesseract reports whether a tesseract binary is resolvable.
func HasTesseract() bool { return TesseractPath() != "" }

// HasExiftool reports whether an exiftool binary is resolvable.
func HasExiftool() bool { return ExiftoolPath() != "" }

// Tools is the host-tool matrix the agent advertises (and the local status page
// renders). Keys are stable identifiers central's console gates on; a false
// value means "this host cannot do the capability that tool backs".
func Tools() map[string]bool {
	return map[string]bool{
		"ffmpeg":    HasFFmpeg(),
		"ffprobe":   HasFFprobe(),
		"tesseract": HasTesseract(),
		"exiftool":  HasExiftool(),
	}
}

// ExtractFormats lists the taxonomy file_category values this build can actually
// extract ON THIS HOST — compiled-in support intersected with detected tools.
// It is the honest answer to "what will extraction produce here", which is what
// the console shows next to the policy editor.
func ExtractFormats() []string { return extract.Formats(HasFFprobe()) }
