package install

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"time"
)

// ErrNeedAdmin is returned when install/uninstall is attempted without the
// elevation the service registration requires.
var ErrNeedAdmin = errors.New("administrator/root privileges are required (re-run elevated: Windows 'Run as administrator', Linux/macOS 'sudo')")

// Status is the service manager's view of the service, normalised across OSes.
type Status int

const (
	StatusUnknown Status = iota
	StatusRunning
	StatusStopped
	StatusNotInstalled
)

// Controller is the thin service-manager surface the installer drives. The real
// implementation wraps kardianos/service; tests inject a mock so no service is
// actually registered during unit tests.
type Controller interface {
	Install() error
	Uninstall() error
	Start() error
	Stop() error
	Restart() error
	Status() (Status, error)
}

// FS abstracts the filesystem side effects so install/uninstall decisions are
// testable against an in-memory fake.
type FS interface {
	MkdirAll(path string, perm os.FileMode) error
	CopyFile(src, dst string, perm os.FileMode) error
	Remove(path string) error
	RemoveAll(path string) error
	// SameFile reports whether src and dst resolve to the same on-disk file, so a
	// re-install whose source binary already IS the installed binary skips the
	// self-overwrite.
	SameFile(src, dst string) (bool, error)
}

// Installer performs the idempotent install / uninstall using injected effects.
type Installer struct {
	Layout    Layout
	SourceExe string // path to the running binary to copy into place
	FS        FS
	Service   Controller

	// IsAdmin reports whether the current process is elevated. Required.
	IsAdmin func() bool
	// Enrolled reports whether the agent already has an on-disk identity. When
	// nil the installer treats the agent as not-yet-enrolled.
	Enrolled func() bool
	// Enroll runs the non-interactive enroll flow. Called only when HasToken is
	// true and the agent is not already enrolled. Nil disables enrollment.
	Enroll   func() error
	HasToken bool

	// AdoptFrom is a candidate data dir holding an EXISTING enrollment (the
	// invoking user's per-user default dir). When the target data dir has no
	// identity and no token is available, install copies that enrollment —
	// identity, index, outbox, scan config — into the system layout so a host
	// promoted from "ran it manually" to "service install" keeps working.
	// Empty disables adoption.
	AdoptFrom string
	// Adopted is set by Install when an adoption actually happened (caller
	// reporting).
	Adopted bool

	Log *slog.Logger
}

// verifySleep is the poll delay used by verifyRunning — a package var so the
// unit tests run instantly.
var verifySleep = time.Sleep

func (in *Installer) log() *slog.Logger {
	if in.Log != nil {
		return in.Log
	}
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

// vlog emits a verbose-level record (the install seams the user asked to be
// able to see without the full debug firehose).
func (in *Installer) vlog(msg string, args ...any) {
	in.log().Log(context.Background(), verboseLevel, msg, args...)
}

// dirPerm returns the create mode for each install directory. Data holds private
// keys so it is owner-only on POSIX; the rest are world-readable dirs.
func (in *Installer) dirs() []struct {
	path string
	perm os.FileMode
} {
	return []struct {
		path string
		perm os.FileMode
	}{
		{in.Layout.InstallDir, 0o755},
		{in.Layout.DataDir, 0o700},
		{in.Layout.ConfigDir, 0o755},
		{in.Layout.LogDir, 0o755},
	}
}

// Install lays out the install tree, (re)places the binary, optionally enrolls,
// and registers + starts the service. It is idempotent: a re-run stops and
// deregisters any existing service first, then re-registers with the current
// configuration and starts it (in-place upgrade). Requires elevation.
func (in *Installer) Install() error {
	if in.IsAdmin == nil || !in.IsAdmin() {
		return ErrNeedAdmin
	}
	for _, d := range in.dirs() {
		if err := in.FS.MkdirAll(d.path, d.perm); err != nil {
			return fmt.Errorf("create %s: %w", d.path, err)
		}
	}
	in.vlog("install layout created", "install_dir", in.Layout.InstallDir, "data_dir", in.Layout.DataDir, "log_dir", in.Layout.LogDir)

	// (b) idempotency + the Windows exe lock: an existing service must be
	// STOPPED — and its process fully exited — BEFORE the binary copy. Windows
	// holds a mandatory lock on a running service's executable, so the old
	// order (copy first, stop later) made every in-place upgrade with the
	// service running fail at the copy with "being used by another process"
	// (live 2026-08-05).
	if st, err := in.Service.Status(); err == nil && st != StatusNotInstalled {
		in.vlog("existing service found; stopping + deregistering for in-place upgrade", "status", st)
		_ = in.Service.Stop()
		in.waitStopped()
		_ = in.Service.Uninstall()
	}

	// (c) place the binary unless the source already IS the target (re-running
	// `install` from the installed path).
	same, _ := in.FS.SameFile(in.SourceExe, in.Layout.BinPath)
	if same {
		in.vlog("binary already in place; skipping copy", "path", in.Layout.BinPath)
	} else {
		if err := in.FS.CopyFile(in.SourceExe, in.Layout.BinPath, 0o755); err != nil {
			return fmt.Errorf("copy binary to %s: %w (if the service is still stopping, wait a moment and re-run)", in.Layout.BinPath, err)
		}
		in.vlog("binary copied", "from", in.SourceExe, "to", in.Layout.BinPath)
	}

	// (c0) adopt an existing per-user enrollment. A host that first ran the
	// agent manually (identity under the invoking user's config dir) and is
	// then promoted to a service install would otherwise register a service
	// pointing at an EMPTY system data dir — it dies on start with "no
	// enrolled identity" while install reports success (live 2026-08-04, a
	// Windows agent). Copies everything except logs/ (identity, local index,
	// outbox, scan config) so replication sequence continuity is preserved;
	// never overwrites existing target files.
	if in.AdoptFrom != "" && (in.Enrolled == nil || !in.Enrolled()) {
		adopted, aerr := AdoptData(in.AdoptFrom, in.Layout.DataDir)
		if aerr != nil {
			return fmt.Errorf(
				"adopt existing enrollment from %s: %w (stop any manually-running filearr-agent and re-run install)",
				in.AdoptFrom, aerr)
		}
		if adopted {
			in.Adopted = true
			in.log().Info("adopted existing enrollment",
				"from", in.AdoptFrom, "to", in.Layout.DataDir)
		}
	}

	// (c) non-interactive enroll when a token is present and we are not enrolled,
	// BEFORE the service starts so it comes up already-enrolled.
	if in.HasToken && in.Enroll != nil {
		enrolled := in.Enrolled != nil && in.Enrolled()
		if enrolled {
			in.vlog("agent already enrolled; skipping enroll during install")
		} else {
			in.log().Info("enrolling agent during install")
			if err := in.Enroll(); err != nil {
				return fmt.Errorf("enroll during install: %w", err)
			}
		}
	}

	// (d) register + start.
	if err := in.Service.Install(); err != nil {
		return fmt.Errorf("register service: %w", err)
	}
	if err := in.Service.Start(); err != nil {
		return fmt.Errorf("start service: %w", err)
	}
	// (f) verify the service actually STAYS up. "Started" from the service
	// manager only means the start was dispatched; a service that dies in its
	// first seconds (classic: empty data dir, locked index) otherwise yields a
	// success banner over a dead service (live 2026-08-04).
	if err := in.verifyRunning(); err != nil {
		return err
	}
	in.log().Info("service installed and started")
	return nil
}

// waitStopped polls briefly after Stop until the service reports stopped (or
// gone): Stop() returning only means the stop was REQUESTED — the process may
// still be exiting and holding its exe lock. Best-effort: on timeout the copy
// simply fails with its own actionable error.
func (in *Installer) waitStopped() {
	for i := 0; i < 20; i++ {
		st, err := in.Service.Status()
		if err != nil || st == StatusStopped || st == StatusNotInstalled {
			return
		}
		verifySleep(500 * time.Millisecond)
	}
}

// verifyRunning polls the service state briefly after Start and fails with an
// actionable message when it exits instead of running.
func (in *Installer) verifyRunning() error {
	running := 0
	for i := 0; i < 12; i++ {
		st, err := in.Service.Status()
		switch {
		case err == nil && st == StatusRunning:
			running++
			if running >= 3 {
				return nil // stable across ~1.5s of polls
			}
		case err == nil && st == StatusStopped:
			return fmt.Errorf(
				"service exited immediately after start — check the OS event log and %s; "+
					"if there is no enrolled identity in %s, enroll first "+
					"(`filearr-agent enroll -token <token> -data %q`) and re-run install",
				in.Layout.LogDir, in.Layout.DataDir, in.Layout.DataDir)
		default:
			running = 0
		}
		verifySleep(500 * time.Millisecond)
	}
	return fmt.Errorf(
		"service did not reach a stable running state — check the OS event log and %s",
		in.Layout.LogDir)
}

// AdoptData copies an existing agent data dir (identity + index + outbox +
// scan config; logs excluded) into dst. No-op (false, nil) when src equals
// dst, src has no enrollment (no state.json), or dst already has one.
// Existing destination files are never overwritten.
func AdoptData(src, dst string) (bool, error) {
	if src == "" || dst == "" || filepath.Clean(src) == filepath.Clean(dst) {
		return false, nil
	}
	if _, err := os.Stat(filepath.Join(src, "state.json")); err != nil {
		return false, nil // nothing to adopt
	}
	if _, err := os.Stat(filepath.Join(dst, "state.json")); err == nil {
		return false, nil // target already enrolled; never clobber
	}
	err := filepath.WalkDir(src, func(path string, d os.DirEntry, werr error) error {
		if werr != nil {
			return werr
		}
		rel, rerr := filepath.Rel(src, path)
		if rerr != nil {
			return rerr
		}
		if rel == "." {
			return nil
		}
		// Logs stay per-location; everything else moves.
		if d.IsDir() && d.Name() == "logs" && filepath.Dir(rel) == "." {
			return filepath.SkipDir
		}
		target := filepath.Join(dst, rel)
		if d.IsDir() {
			return os.MkdirAll(target, 0o700)
		}
		if _, serr := os.Stat(target); serr == nil {
			return nil // never overwrite
		}
		buf, cerr := os.ReadFile(path)
		if cerr != nil {
			return fmt.Errorf("read %s: %w", path, cerr)
		}
		if werr := os.WriteFile(target, buf, 0o600); werr != nil {
			return fmt.Errorf("write %s: %w", target, werr)
		}
		return nil
	})
	if err != nil {
		return false, err
	}
	return true, nil
}

// Uninstall stops + deregisters the service and removes the installed binary.
// When purge is false the data/config/log directories are KEPT and returned so
// the caller can report them; purge additionally removes them. Requires
// elevation.
func (in *Installer) Uninstall(purge bool) (kept []string, err error) {
	if in.IsAdmin == nil || !in.IsAdmin() {
		return nil, ErrNeedAdmin
	}
	if st, serr := in.Service.Status(); serr == nil && st != StatusNotInstalled {
		_ = in.Service.Stop()
		if err := in.Service.Uninstall(); err != nil {
			return nil, fmt.Errorf("deregister service: %w", err)
		}
		in.vlog("service stopped + deregistered")
	}
	if err := in.FS.Remove(in.Layout.BinPath); err != nil && !os.IsNotExist(err) {
		return nil, fmt.Errorf("remove binary %s: %w", in.Layout.BinPath, err)
	}
	if purge {
		for _, d := range []string{in.Layout.DataDir, in.Layout.LogDir, in.Layout.ConfigDir} {
			if err := in.FS.RemoveAll(d); err != nil {
				return nil, fmt.Errorf("purge %s: %w", d, err)
			}
		}
		in.log().Info("service uninstalled; data/logs/config purged")
		return nil, nil
	}
	// Dedup ConfigDir==DataDir (Windows/macOS share one dir).
	kept = dedup([]string{in.Layout.DataDir, in.Layout.ConfigDir, in.Layout.LogDir})
	in.log().Info("service uninstalled; data/logs/config kept", "kept", kept)
	return kept, nil
}

func dedup(in []string) []string {
	seen := map[string]bool{}
	var out []string
	for _, s := range in {
		if s == "" || seen[s] {
			continue
		}
		seen[s] = true
		out = append(out, s)
	}
	return out
}

// verboseLevel duplicates agentlog.LevelVerbose to keep this package free of a
// dependency on agentlog (which pulls lumberjack/term). Kept numerically in sync.
const verboseLevel = slog.Level(-2)
