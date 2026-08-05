package install

import (
	"strings"
	"path/filepath"
	"time"
	"errors"
	"os"
	"testing"
)

// mockController records lifecycle calls and returns a scripted status.
type mockController struct {
	status     Status
	statusErr  error
	// postStartStatus, when non-zero, is what Start() sets instead of Running
	// (models a service that dies right after a "successful" start).
	postStartStatus Status
	calls      []string
	installErr error
	startErr   error
}

func (m *mockController) record(op string) { m.calls = append(m.calls, op) }

func (m *mockController) Install() error   { m.record("install"); return m.installErr }
func (m *mockController) Uninstall() error { m.record("uninstall"); return nil }
func (m *mockController) Start() error {
	m.record("start")
	// A successful start brings the service to Running (what verifyRunning
	// polls for); a start error leaves the prior status.
	if m.startErr == nil {
		m.status = StatusRunning
		if m.postStartStatus != StatusUnknown {
			m.status = m.postStartStatus
		}
	}
	return m.startErr
}
func (m *mockController) Stop() error {
	m.record("stop")
	// A real stop leaves the service observable as Stopped (what waitStopped
	// polls for before the binary copy).
	m.status = StatusStopped
	return nil
}
func (m *mockController) Restart() error   { m.record("restart"); return nil }
func (m *mockController) Status() (Status, error) {
	m.record("status")
	return m.status, m.statusErr
}

// fakeFS records filesystem effects in memory.
type fakeFS struct {
	dirs     map[string]bool
	copied   map[string]bool
	removed  map[string]bool
	removedA map[string]bool
	same     bool // SameFile return
}

func newFakeFS() *fakeFS {
	return &fakeFS{dirs: map[string]bool{}, copied: map[string]bool{}, removed: map[string]bool{}, removedA: map[string]bool{}}
}

func (f *fakeFS) MkdirAll(path string, _ os.FileMode) error { f.dirs[path] = true; return nil }
func (f *fakeFS) CopyFile(_, dst string, _ os.FileMode) error {
	f.copied[dst] = true
	return nil
}
func (f *fakeFS) Remove(path string) error    { f.removed[path] = true; return nil }
func (f *fakeFS) RemoveAll(path string) error { f.removedA[path] = true; return nil }
func (f *fakeFS) SameFile(_, _ string) (bool, error) {
	return f.same, nil
}

func testLayout() Layout {
	l, _ := ResolveLayout("linux", nil)
	return l
}

func TestInstallFreshRegistersAndStarts(t *testing.T) {
	fs := newFakeFS()
	ctrl := &mockController{status: StatusNotInstalled}
	in := &Installer{
		Layout: testLayout(), SourceExe: "/tmp/self", FS: fs, Service: ctrl,
		IsAdmin: func() bool { return true },
	}
	if err := in.Install(); err != nil {
		t.Fatalf("Install: %v", err)
	}
	// Dirs created.
	for _, d := range []string{"/usr/local/bin", "/var/lib/filearr-agent", "/etc/filearr-agent", "/var/log/filearr-agent"} {
		if !fs.dirs[d] {
			t.Fatalf("dir %s not created", d)
		}
	}
	// Binary copied.
	if !fs.copied["/usr/local/bin/filearr-agent"] {
		t.Fatal("binary not copied")
	}
	// Fresh install: no stop/uninstall, then install + start.
	assertCalls(t, ctrl.calls, []string{"status", "install", "start", "status", "status", "status"})
}

func TestInstallIdempotentUpgradeStopsFirst(t *testing.T) {
	fs := newFakeFS()
	ctrl := &mockController{status: StatusRunning} // already installed + running
	in := &Installer{
		Layout: testLayout(), SourceExe: "/tmp/self", FS: fs, Service: ctrl,
		IsAdmin: func() bool { return true },
	}
	if err := in.Install(); err != nil {
		t.Fatalf("Install: %v", err)
	}
	// Existing service: stop + uninstall before re-install + start.
	assertCalls(t, ctrl.calls, []string{"status", "stop", "status", "uninstall", "install", "start", "status", "status", "status"})
}

func TestInstallSkipsCopyWhenSameFile(t *testing.T) {
	fs := newFakeFS()
	fs.same = true
	ctrl := &mockController{status: StatusNotInstalled}
	in := &Installer{
		Layout: testLayout(), SourceExe: "/usr/local/bin/filearr-agent", FS: fs, Service: ctrl,
		IsAdmin: func() bool { return true },
	}
	if err := in.Install(); err != nil {
		t.Fatalf("Install: %v", err)
	}
	if fs.copied["/usr/local/bin/filearr-agent"] {
		t.Fatal("binary should not be copied onto itself")
	}
}

func TestInstallEnrollGating(t *testing.T) {
	t.Run("token present + not enrolled => enroll called", func(t *testing.T) {
		fs := newFakeFS()
		ctrl := &mockController{status: StatusNotInstalled}
		enrolled := 0
		in := &Installer{
			Layout: testLayout(), SourceExe: "/tmp/self", FS: fs, Service: ctrl,
			IsAdmin:  func() bool { return true },
			HasToken: true,
			Enrolled: func() bool { return false },
			Enroll:   func() error { enrolled++; return nil },
		}
		if err := in.Install(); err != nil {
			t.Fatal(err)
		}
		if enrolled != 1 {
			t.Fatalf("enroll called %d times, want 1", enrolled)
		}
	})

	t.Run("token present + already enrolled => enroll skipped", func(t *testing.T) {
		fs := newFakeFS()
		ctrl := &mockController{status: StatusNotInstalled}
		enrolled := 0
		in := &Installer{
			Layout: testLayout(), SourceExe: "/tmp/self", FS: fs, Service: ctrl,
			IsAdmin:  func() bool { return true },
			HasToken: true,
			Enrolled: func() bool { return true },
			Enroll:   func() error { enrolled++; return nil },
		}
		if err := in.Install(); err != nil {
			t.Fatal(err)
		}
		if enrolled != 0 {
			t.Fatalf("enroll called %d times, want 0 (already enrolled)", enrolled)
		}
	})

	t.Run("no token => enroll skipped", func(t *testing.T) {
		fs := newFakeFS()
		ctrl := &mockController{status: StatusNotInstalled}
		enrolled := 0
		in := &Installer{
			Layout: testLayout(), SourceExe: "/tmp/self", FS: fs, Service: ctrl,
			IsAdmin:  func() bool { return true },
			HasToken: false,
			Enroll:   func() error { enrolled++; return nil },
		}
		if err := in.Install(); err != nil {
			t.Fatal(err)
		}
		if enrolled != 0 {
			t.Fatalf("enroll called %d times, want 0 (no token)", enrolled)
		}
	})
}

func TestInstallRequiresAdmin(t *testing.T) {
	fs := newFakeFS()
	ctrl := &mockController{status: StatusNotInstalled}
	in := &Installer{
		Layout: testLayout(), SourceExe: "/tmp/self", FS: fs, Service: ctrl,
		IsAdmin: func() bool { return false },
	}
	if err := in.Install(); !errors.Is(err, ErrNeedAdmin) {
		t.Fatalf("Install without admin: err=%v, want ErrNeedAdmin", err)
	}
	if len(ctrl.calls) != 0 || len(fs.dirs) != 0 {
		t.Fatal("no side effects should occur without admin")
	}
}

func TestUninstallKeepsDataByDefault(t *testing.T) {
	fs := newFakeFS()
	ctrl := &mockController{status: StatusRunning}
	in := &Installer{
		Layout: testLayout(), FS: fs, Service: ctrl,
		IsAdmin: func() bool { return true },
	}
	kept, err := in.Uninstall(false)
	if err != nil {
		t.Fatal(err)
	}
	assertCalls(t, ctrl.calls, []string{"status", "stop", "uninstall"})
	if !fs.removed["/usr/local/bin/filearr-agent"] {
		t.Fatal("binary not removed")
	}
	if len(fs.removedA) != 0 {
		t.Fatalf("data/logs/config should be kept, but RemoveAll hit: %v", fs.removedA)
	}
	if len(kept) == 0 {
		t.Fatal("expected kept dirs to be reported")
	}
}

func TestUninstallPurgeRemovesEverything(t *testing.T) {
	fs := newFakeFS()
	ctrl := &mockController{status: StatusStopped}
	in := &Installer{
		Layout: testLayout(), FS: fs, Service: ctrl,
		IsAdmin: func() bool { return true },
	}
	kept, err := in.Uninstall(true)
	if err != nil {
		t.Fatal(err)
	}
	if len(kept) != 0 {
		t.Fatalf("purge should keep nothing, got %v", kept)
	}
	for _, d := range []string{"/var/lib/filearr-agent", "/var/log/filearr-agent", "/etc/filearr-agent"} {
		if !fs.removedA[d] {
			t.Fatalf("purge did not remove %s", d)
		}
	}
}

func TestUninstallNotInstalledSkipsServiceUninstall(t *testing.T) {
	fs := newFakeFS()
	ctrl := &mockController{status: StatusNotInstalled}
	in := &Installer{
		Layout: testLayout(), FS: fs, Service: ctrl,
		IsAdmin: func() bool { return true },
	}
	if _, err := in.Uninstall(false); err != nil {
		t.Fatal(err)
	}
	// Only Status queried; no stop/uninstall on an absent service.
	assertCalls(t, ctrl.calls, []string{"status"})
}

func assertCalls(t *testing.T, got, want []string) {
	t.Helper()
	if len(got) != len(want) {
		t.Fatalf("calls=%v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("calls=%v, want %v", got, want)
		}
	}
}

// The post-start verification polls with real sleeps in production; tests run
// it instantly and model a controller whose Start actually brings the service
// up (see mockController.Start).
func init() {
	verifySleep = func(_ time.Duration) {}
}

// --- adoption (live 2026-08-04: manual per-user enrollment + service install)

func writeTree(t *testing.T, root string, files map[string]string) {
	t.Helper()
	for rel, content := range files {
		p := filepath.Join(root, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(p), 0o700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(content), 0o600); err != nil {
			t.Fatal(err)
		}
	}
}

func TestAdoptDataCopiesEnrollmentSkipsLogs(t *testing.T) {
	src, dst := t.TempDir(), t.TempDir()
	writeTree(t, src, map[string]string{
		"state.json":     `{"agent_id":"a"}`,
		"agent.key":      "KEY",
		"agent.crt":      "CRT",
		"roots.pem":      "ROOTS",
		"scan.json":      `{"roots":["d:/"]}`,
		"items.db":       "SQLITE",
		"logs/agent.log": "old logs stay behind",
	})
	adopted, err := AdoptData(src, dst)
	if err != nil || !adopted {
		t.Fatalf("AdoptData = %v, %v; want adopted", adopted, err)
	}
	for _, rel := range []string{"state.json", "agent.key", "agent.crt", "roots.pem", "scan.json", "items.db"} {
		if _, err := os.Stat(filepath.Join(dst, rel)); err != nil {
			t.Errorf("expected %s adopted: %v", rel, err)
		}
	}
	if _, err := os.Stat(filepath.Join(dst, "logs", "agent.log")); err == nil {
		t.Error("logs must NOT be adopted")
	}
	// Source stays untouched (rollback safety).
	if _, err := os.Stat(filepath.Join(src, "state.json")); err != nil {
		t.Error("source enrollment must be left in place")
	}
}

func TestAdoptDataNoOps(t *testing.T) {
	src, dst := t.TempDir(), t.TempDir()
	// no enrollment at src
	if adopted, err := AdoptData(src, dst); err != nil || adopted {
		t.Fatalf("empty src: adopted=%v err=%v, want no-op", adopted, err)
	}
	// target already enrolled: never clobber
	writeTree(t, src, map[string]string{"state.json": "src"})
	writeTree(t, dst, map[string]string{"state.json": "dst"})
	if adopted, err := AdoptData(src, dst); err != nil || adopted {
		t.Fatalf("enrolled dst: adopted=%v err=%v, want no-op", adopted, err)
	}
	if b, _ := os.ReadFile(filepath.Join(dst, "state.json")); string(b) != "dst" {
		t.Error("existing target enrollment was clobbered")
	}
	// src == dst
	if adopted, err := AdoptData(src, src); err != nil || adopted {
		t.Fatalf("src==dst: adopted=%v err=%v, want no-op", adopted, err)
	}
}

func TestInstallFailsWhenServiceDiesAfterStart(t *testing.T) {
	// A start that "succeeds" but leaves the service stopped (dead on arrival —
	// e.g. empty data dir) must FAIL the install with guidance, not print a
	// success banner (live 2026-08-04).
	fs := newFakeFS()
	ctrl := &mockController{status: StatusNotInstalled, postStartStatus: StatusStopped}
	in := &Installer{
		Layout: testLayout(), SourceExe: "/tmp/self", FS: fs, Service: ctrl,
		IsAdmin: func() bool { return true },
	}
	err := in.Install()
	if err == nil {
		t.Fatal("want install failure when the service exits immediately")
	}
	if !strings.Contains(err.Error(), "exited immediately") || !strings.Contains(err.Error(), in.Layout.DataDir) {
		t.Fatalf("error must explain + name the data dir, got: %v", err)
	}
}

func TestAdoptDataStampsSourceMarker(t *testing.T) {
	src, dst := t.TempDir(), t.TempDir()
	writeTree(t, src, map[string]string{"state.json": `{"agent_id":"a"}`})
	if adopted, err := AdoptData(src, dst); err != nil || !adopted {
		t.Fatalf("adopt: %v %v", adopted, err)
	}
	if got := AdoptedTo(src); got != dst {
		t.Fatalf("AdoptedTo(src) = %q, want %q", got, dst)
	}
	// The marker never travels with the copy, and the target is not marked.
	if _, err := os.Stat(filepath.Join(dst, AdoptedMarkerName)); err == nil {
		t.Fatal("marker must not be copied/created at the target")
	}
	if got := AdoptedTo(dst); got != "" {
		t.Fatalf("AdoptedTo(dst) = %q, want empty", got)
	}
}
