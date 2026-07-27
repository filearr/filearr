package agentlog

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TailFiles is the web UI's cross-process log surface: the daemon, scans, and
// the container entrypoint each write their own file (lumberjack is not
// multi-process safe on a shared path) and the Logs panel needs them merged
// back into one timestamp-ordered view.
func TestTailFilesMergesProcessesByTimestamp(t *testing.T) {
	dir := t.TempDir()
	write := func(name string, lines ...string) {
		t.Helper()
		if err := os.WriteFile(filepath.Join(dir, name), []byte(strings.Join(lines, "\n")+"\n"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	write("filearr-agent.log",
		`time=2026-07-27T05:00:01.100Z level=INFO msg="daemon a"`,
		`time=2026-07-27T05:00:04.000Z level=INFO msg="daemon b"`,
	)
	write("filearr-agent-scan.log",
		`time=2026-07-27T05:00:02.500Z level=INFO msg="scan progress" seen=250`,
		`time=2026-07-27T05:00:05.000Z level=WARN msg="hash timed out" path=/x`,
	)
	// entrypoint format: bare RFC3339 prefix, no time= attr
	write("filearr-agent-entrypoint.log",
		`2026-07-27T05:00:00Z [entrypoint] starting scan loop`,
	)
	write("notes.txt", "not a log file, must be ignored")

	got := TailFiles(dir, 500)
	var wantOrder = []string{"starting scan loop", "daemon a", "scan progress", "daemon b", "hash timed out"}
	if len(got) != 5 {
		t.Fatalf("want 5 merged lines, got %d: %q", len(got), got)
	}
	for i, frag := range wantOrder {
		if !strings.Contains(got[i], frag) {
			t.Fatalf("merged line %d = %q, want fragment %q (full: %q)", i, got[i], frag, got)
		}
	}

	// limit keeps the NEWEST lines
	got = TailFiles(dir, 2)
	if len(got) != 2 || !strings.Contains(got[1], "hash timed out") || !strings.Contains(got[0], "daemon b") {
		t.Fatalf("limit=2 should keep the two newest, got %q", got)
	}
}

func TestTailFilesMissingDir(t *testing.T) {
	if lines := TailFiles(filepath.Join(t.TempDir(), "nope"), 100); lines != nil {
		t.Fatalf("missing dir must yield nil, got %q", lines)
	}
}
