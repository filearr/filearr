// Package agentlog builds the agent's slog logger with a user-selectable level
// and optional rotating file output.
//
// Five user-facing level names map onto slog levels. slog is "lower value = more
// verbose", and a handler emits every record whose level is >= the handler's
// threshold, so the ordering below is deliberate:
//
//	name     slog.Level          shown when threshold <=
//	error    slog.LevelError (8)  error
//	warn     slog.LevelWarn  (4)  warn, error
//	info     slog.LevelInfo  (0)  info, warn, error            (default)
//	verbose  LevelVerbose   (-2)  verbose, info, warn, error
//	debug    slog.LevelDebug(-4)  everything
//
// "verbose" sits strictly between info and debug: it surfaces the extra
// operational seams (service lifecycle, sidecar resolution, install steps)
// without the full debug firehose. A handler set to info therefore hides verbose
// AND debug; a handler set to verbose shows verbose but still hides debug.
package agentlog

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"

	"golang.org/x/term"
	lumberjack "gopkg.in/natefinch/lumberjack.v2"
)

// LevelVerbose is the custom slog level between Info (0) and Debug (-4).
const LevelVerbose = slog.Level(-2)

// LogFileName is the fixed rotating log file name under a configured log dir.
const LogFileName = "filearr-agent.log"

// Rotation parameters (research: keep the on-disk footprint bounded on a small
// appliance while retaining enough history to diagnose a restart loop).
const (
	rotateMaxSizeMiB = 10 // lumberjack MaxSize is in MiB
	rotateMaxBackups = 5
	rotateCompress   = true
)

// ParseLevel maps a user-facing level name (case-insensitive) to its slog level.
// An empty string yields the info default with ok=true; an unrecognised name
// yields ok=false so the caller can report the bad value rather than silently
// defaulting.
func ParseLevel(name string) (slog.Level, bool) {
	switch strings.ToLower(strings.TrimSpace(name)) {
	case "":
		return slog.LevelInfo, true
	case "error":
		return slog.LevelError, true
	case "warn", "warning":
		return slog.LevelWarn, true
	case "info":
		return slog.LevelInfo, true
	case "verbose":
		return LevelVerbose, true
	case "debug":
		return slog.LevelDebug, true
	default:
		return slog.LevelInfo, false
	}
}

// Options configures New.
type Options struct {
	// Level is the resolved threshold (default slog.LevelInfo for the zero value).
	Level slog.Level
	// LogDir, when non-empty, enables the rotating file sink at
	// <LogDir>/<FileName>. The directory is created if missing.
	LogDir string
	// FileName overrides the file sink's name (default LogFileName). Concurrent
	// agent PROCESSES (the run daemon + a scan invocation in a container) must
	// each write their own file — lumberjack rotation is not multi-process safe
	// on a shared path — so the dispatcher derives a per-command name and the
	// web UI merges the files back together with TailFiles.
	FileName string
	// Stderr forces stderr output. When a file sink is active, stderr is added
	// only if this is true AND stderr is a terminal (so a service run does not
	// duplicate every line into a captured stderr). When no file sink is active,
	// stderr is always used regardless of this flag.
	Stderr bool
	// ForceStderr keeps stderr output alongside a file sink even when stderr is
	// NOT a terminal. A container needs this: its stderr IS the `docker logs`
	// stream, which must not go dark just because a shared log dir is set.
	ForceStderr bool
}

// New builds a *slog.Logger and an io.Closer for any file sink (nil-safe to
// close; a no-op when only stderr is used). The custom VERBOSE level renders as
// "VERBOSE" in the text handler rather than slog's default "DEBUG+2".
func New(opts Options) (*slog.Logger, io.Closer, error) {
	var writers []io.Writer
	var closer io.Closer = noopCloser{}

	if opts.LogDir != "" {
		if err := os.MkdirAll(opts.LogDir, 0o755); err != nil {
			return nil, nil, fmt.Errorf("create log dir %s: %w", opts.LogDir, err)
		}
		name := opts.FileName
		if name == "" {
			name = LogFileName
		}
		lj := &lumberjack.Logger{
			Filename:   filepath.Join(opts.LogDir, name),
			MaxSize:    rotateMaxSizeMiB,
			MaxBackups: rotateMaxBackups,
			Compress:   rotateCompress,
		}
		writers = append(writers, lj)
		closer = lj
		// A tty attachment gets a live echo alongside the file; a container
		// forces the echo so `docker logs` keeps carrying every line.
		if opts.ForceStderr || (opts.Stderr && term.IsTerminal(int(os.Stderr.Fd()))) {
			writers = append(writers, os.Stderr)
		}
	} else {
		// No file sink: stderr is the only output (matches the historic default).
		writers = append(writers, os.Stderr)
	}

	var w io.Writer
	if len(writers) == 1 {
		w = writers[0]
	} else {
		w = io.MultiWriter(writers...)
	}
	// Tee every rendered line into the in-process ring so the local web UI's
	// Logs panel can show recent activity without touching the file sink (a
	// container sets no log dir — its lines only ever went to stderr).
	w = ringWriter{inner: w}

	handler := slog.NewTextHandler(w, &slog.HandlerOptions{
		Level:       opts.Level,
		ReplaceAttr: replaceLevel,
	})
	return slog.New(handler), closer, nil
}

// --- recent-logs ring (web UI /api/logs) ------------------------------------
// A tiny, process-global bounded buffer of the most recent RENDERED log lines
// (post level-filtering, so it mirrors exactly what the operator's configured
// level emits). Read-only consumers get a copy; writers never block on it.

// RingSize bounds the retained line count (~500 lines ≈ a few minutes of
// info-level activity; enough to see "why is it doing that" from a browser).
const RingSize = 500

var (
	ringMu    sync.Mutex
	ringLines []string
)

type ringWriter struct{ inner io.Writer }

func (rw ringWriter) Write(p []byte) (int, error) {
	if lines := strings.Split(strings.TrimRight(string(p), "\n"), "\n"); len(lines) > 0 {
		ringMu.Lock()
		ringLines = append(ringLines, lines...)
		if over := len(ringLines) - RingSize; over > 0 {
			ringLines = append(ringLines[:0:0], ringLines[over:]...)
		}
		ringMu.Unlock()
	}
	return rw.inner.Write(p)
}

// Recent returns a copy of the retained lines, oldest first.
func Recent() []string {
	ringMu.Lock()
	defer ringMu.Unlock()
	return append([]string(nil), ringLines...)
}

// Per-file read window for TailFiles: at least 256 KiB, scaled up for deep
// requests (~512 bytes/line budget, generous for slog text), capped at 4 MiB
// so a hostile ?limit can't make the UI read the whole rotation set into RAM.
const (
	tailReadMinBytes = 256 * 1024
	tailReadMaxBytes = 4 * 1024 * 1024
)

// TailFiles merges the tails of every uncompressed *.log file under dir
// (daemon + scan + any other agent process, including lumberjack's rotated
// backups) into one timestamp-ordered view, returning at most limit lines,
// oldest first. This is the cross-PROCESS log surface for the web UI: the
// in-process ring only ever sees the daemon's own lines, but a containerized
// agent runs scans as separate processes whose lines land only in their file.
// Best-effort throughout — an unreadable dir or file yields what the rest
// provides. Sorting keys off each line's leading timestamp (slog's `time=`
// attr or a bare RFC3339 prefix, e.g. the container entrypoint); keyless lines
// sort with their neighbors via stable sort.
func TailFiles(dir string, limit int) []string {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil
	}
	readBytes := int64(limit) * 512
	if readBytes < tailReadMinBytes {
		readBytes = tailReadMinBytes
	}
	if readBytes > tailReadMaxBytes {
		readBytes = tailReadMaxBytes
	}
	type keyed struct{ key, line string }
	var all []keyed
	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".log") {
			continue
		}
		lines := tailLines(filepath.Join(dir, e.Name()), limit, readBytes)
		key := ""
		for _, ln := range lines {
			if k := lineTimeKey(ln); k != "" {
				key = k // keyless continuation lines inherit the last timestamp
			}
			all = append(all, keyed{key: key, line: ln})
		}
	}
	sort.SliceStable(all, func(i, j int) bool { return all[i].key < all[j].key })
	if over := len(all) - limit; over > 0 {
		all = all[over:]
	}
	out := make([]string, len(all))
	for i, k := range all {
		out[i] = k.line
	}
	return out
}

// tailLines returns up to limit trailing lines of one file, reading at most
// maxBytes from its end (a partial first line after the seek is dropped).
func tailLines(path string, limit int, maxBytes int64) []string {
	f, err := os.Open(path)
	if err != nil {
		return nil
	}
	defer f.Close()
	st, err := f.Stat()
	if err != nil {
		return nil
	}
	off, partial := int64(0), false
	if st.Size() > maxBytes {
		off, partial = st.Size()-maxBytes, true
	}
	buf := make([]byte, st.Size()-off)
	if _, err := f.ReadAt(buf, off); err != nil && err != io.EOF {
		return nil
	}
	lines := strings.Split(strings.TrimRight(string(buf), "\n"), "\n")
	if partial && len(lines) > 0 {
		lines = lines[1:]
	}
	if len(lines) == 1 && lines[0] == "" {
		return nil
	}
	if over := len(lines) - limit; over > 0 {
		lines = lines[over:]
	}
	return lines
}

// lineTimeKey extracts a lexicographically sortable timestamp from a log line:
// slog text lines start `time=2026-...`; the container entrypoint's lines start
// with a bare RFC3339 stamp. Anything else yields "".
func lineTimeKey(line string) string {
	if rest, ok := strings.CutPrefix(line, "time="); ok {
		if i := strings.IndexByte(rest, ' '); i > 0 {
			return rest[:i]
		}
		return rest
	}
	if len(line) >= 20 && line[4] == '-' && line[7] == '-' && line[10] == 'T' {
		if i := strings.IndexByte(line, ' '); i > 0 {
			return line[:i]
		}
	}
	return ""
}

// replaceLevel renders LevelVerbose as "VERBOSE" (slog would otherwise print
// "DEBUG+2"). Other levels keep their default names.
func replaceLevel(_ []string, a slog.Attr) slog.Attr {
	if a.Key != slog.LevelKey {
		return a
	}
	if lvl, ok := a.Value.Any().(slog.Level); ok && lvl == LevelVerbose {
		a.Value = slog.StringValue("VERBOSE")
	}
	return a
}

// Verbose logs at LevelVerbose (a convenience mirroring slog.Logger.Info/Debug,
// which have no verbose sibling).
func Verbose(log *slog.Logger, msg string, args ...any) {
	if log == nil {
		return
	}
	log.Log(context.Background(), LevelVerbose, msg, args...)
}

type noopCloser struct{}

func (noopCloser) Close() error { return nil }
