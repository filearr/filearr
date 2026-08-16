package main

import (
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"github.com/filearr/filearr/agent/internal/agentlog"
	"github.com/filearr/filearr/agent/internal/enroll"
	"github.com/filearr/filearr/agent/internal/install"
	"github.com/filearr/filearr/agent/internal/inventory"
)

// runInstall installs the agent as a system service: resolve the OS layout,
// place the binary, optionally enroll (when a token is configured and the agent
// is not already enrolled), then register + start an auto-start,
// restart-on-failure service. Idempotent — a re-run upgrades in place. Requires
// administrator/root.
func runInstall(args []string) error {
	fs := newFlagSet("install")
	cfg := bindCommonFlags(fs)
	fs.StringVar(&cfg.Token, "token", envOr(envToken, activeSidecar().EnrollmentToken), "single-use enrollment token (else taken from the sidecar / env)")
	if err := fs.Parse(args); err != nil {
		return err
	}
	set := flagsSet(fs)

	layout, err := install.ResolveLayout(runtime.GOOS, os.Getenv)
	if err != nil {
		return err
	}
	eff := effectiveLayout(cfg, set, layout)

	exe, err := os.Executable()
	if err != nil {
		return fmt.Errorf("resolve current executable: %w", err)
	}

	sc := activeSidecar()
	// The service must be registered with the DURABLE, ABSOLUTE sidecar path
	// (the OS config dir), never the relative download-dir file the operator
	// pointed --config at: a service resolves a relative path against ITS cwd
	// (System32 on Windows) and silently runs without the sidecar.
	sidecarPath := sc.Path
	if sc.Path != "" {
		if p, ierr := sc.InstallTo(eff.ConfigPath); ierr != nil {
			newLogger().Warn("could not place the sidecar in the config dir; the service will read the original path",
				"src", sc.Path, "dst", eff.ConfigPath, "err", ierr)
			if abs, aerr := filepath.Abs(sc.Path); aerr == nil {
				sidecarPath = abs
			}
		} else if p != "" {
			sidecarPath = p
		}
	}
	svcCfg := install.ServiceConfig(eff, sidecarPath, runtime.GOOS)
	ctrl, err := install.NewController(install.NoopProgram{}, svcCfg)
	if err != nil {
		return err
	}

	dataDir := eff.DataDir
	enrollFn := func() error {
		central := cfg.CentralURL
		if central == "" {
			return fmt.Errorf("central URL required to enroll during install (set central_url in the sidecar, -central, or %s)", envCentralURL)
		}
		hostname, _ := os.Hostname()
		if hostname == "" {
			hostname = "filearr-agent"
		}
		name := cfg.Name
		if name == "" {
			name = hostname
		}
		enroller := &enroll.Enroller{
			Central:      enroll.NewCentralClient(central),
			Store:        enroll.NewCertStore(dataDir),
			Token:        cfg.Token,
			Hostname:     hostname,
			Platform:     enroll.DetectPlatform(),
			Name:         name,
			AgentVersion: Version,
		}
		ctx, cancel := signalContext()
		defer cancel()
		res, err := enroller.Enroll(ctx)
		if err != nil {
			return err
		}
		if res.CentralURL != "" && strings.TrimRight(res.CentralURL, "/") != strings.TrimRight(central, "/") {
			newLogger().Info("central directed the daemon to its agent-plane host", "agent_plane", res.CentralURL, "enrolled_via", central)
		}
		// One-shot token contract also applies to install-time enroll: erase the
		// spent token from the sidecar it came from.
		if sc.EnrollmentToken != "" && sc.Path != "" {
			if cerr := sc.ConsumeToken(time.Now()); cerr != nil {
				newLogger().Warn("could not rewrite sidecar to consume the enrollment token", "path", sc.Path, "err", cerr)
			}
		}
		return nil
	}

	inst := &install.Installer{
		Layout:    eff,
		SourceExe: exe,
		FS:        install.OSFS{},
		Service:   ctrl,
		IsAdmin:   install.IsAdmin,
		Enrolled:  func() bool { _, e := enroll.NewCertStore(dataDir).LoadState(); return e == nil },
		Enroll:    enrollFn,
		HasToken:  cfg.Token != "",
		// Promote a manual (per-user) enrollment into the system layout when
		// the target data dir has none: without this, "ran it by hand first,
		// then installed the service" registers a service over an empty data
		// dir that dies on start (live 2026-08-04).
		AdoptFrom: defaultDataDir(),
		Log:       newLogger(),
	}
	if err := inst.Install(); err != nil {
		return err
	}
	fmt.Printf("filearr-agent installed as service %q and started\n", install.ServiceName)
	fmt.Printf("  binary : %s\n", eff.BinPath)
	fmt.Printf("  data   : %s\n", eff.DataDir)
	if inst.Adopted {
		fmt.Printf("  adopted: existing enrollment + local index moved in from %s\n", defaultDataDir())
	}
	fmt.Printf("  logs   : %s\n", eff.LogDir)
	fmt.Printf("  config : %s\n", sidecarPath)
	// Roadmap §20: optional-dependency check, WARN not fail — image/audio/STL
	// thumbnails work without ffmpeg; only video poster-frames need it. The same
	// probe rides the capability advertisement so central's fleet console can
	// show which agents lack it.
	if !inventory.HasFFmpeg() {
		fmt.Println("  note   : ffmpeg was not found on PATH — VIDEO thumbnails will be skipped.")
		fmt.Println("           Install ffmpeg (winget install ffmpeg / apt install ffmpeg /")
		fmt.Println("           brew install ffmpeg) or set FILEARR_AGENT_FFMPEG_PATH; image,")
		fmt.Println("           audio-cover and STL thumbnails work without it.")
	}
	printMissingExtractTools()
	return nil
}

// printMissingExtractTools reports, at install time, which optional extraction
// tools this host lacks and what each one would have bought. Same posture as the
// ffmpeg note above: a WARN, never a failure — extraction is opt-in per policy
// and degrades per capability, so an install on a machine with none of these is
// perfectly valid. Saying it here saves the operator a round trip through the
// console's capability matrix to find out why a policy key looks inert.
func printMissingExtractTools() {
	type toolInfo struct{ tool, buys string }
	tools := []toolInfo{
		{"ffprobe", "video/audio technical probe (codec, resolution, duration, bitrate)"},
		{"exiftool", "deep EXIF (camera, lens, exposure, GPS)"},
		{"pdfinfo", "PDF page count and document properties"},
		{"pdftotext", "PDF body text (RAG chunking + content embeddings)"},
		{"tesseract", "OCR of images and scanned pages"},
		{"pdftoppm", "rasterising scanned PDFs for OCR"},
	}
	present := inventory.Tools()
	versions := inventory.ToolVersions()

	var found, missing []toolInfo
	for _, t := range tools {
		if present[t.tool] {
			found = append(found, t)
		} else {
			missing = append(missing, t)
		}
	}

	// Report what IS here, with versions, before what is not. An operator who
	// just installed the tools wants confirmation that THIS binary found THEM —
	// a version is the only proof that the PATH the service will run with is the
	// one they configured, and it is the fastest way to spot an ancient build
	// (a tesseract 4 reads scans materially worse than a 5.x).
	if len(found) > 0 {
		fmt.Println("  extract: content-extraction tools detected on PATH")
		for _, t := range found {
			if v := versions[t.tool]; v != "" {
				fmt.Printf("           - %-10s %s\n", t.tool, v)
			} else {
				fmt.Printf("           - %-10s (installed; version not reported)\n", t.tool)
			}
		}
	}
	if len(missing) == 0 {
		return
	}
	fmt.Println("  note   : optional content-extraction tools missing from PATH")
	fmt.Println("           (extraction is off until a policy enables it; each tool is independent):")
	for _, m := range missing {
		fmt.Printf("           - %-10s %s\n", m.tool, m.buys)
	}
	fmt.Println("           apt install ffmpeg poppler-utils libimage-exiftool-perl tesseract-ocr")
	fmt.Println("           (or brew install ffmpeg poppler exiftool tesseract; on Windows set")
	fmt.Println("           FILEARR_AGENT_<TOOL>_PATH if the binaries are not on PATH).")
}

// runUninstall stops + deregisters the service and removes the installed binary.
// --purge additionally deletes the data/logs/config directories (default keeps
// them and prints what was kept). Requires administrator/root.
func runUninstall(args []string) error {
	fs := newFlagSet("uninstall")
	cfg := bindCommonFlags(fs)
	purge := fs.Bool("purge", false, "also delete the data, logs, and config directories (default: keep them)")
	if err := fs.Parse(args); err != nil {
		return err
	}
	set := flagsSet(fs)

	layout, err := install.ResolveLayout(runtime.GOOS, os.Getenv)
	if err != nil {
		return err
	}
	eff := effectiveLayout(cfg, set, layout)

	svcCfg := install.ServiceConfig(eff, activeSidecar().Path, runtime.GOOS)
	ctrl, err := install.NewController(install.NoopProgram{}, svcCfg)
	if err != nil {
		return err
	}
	inst := &install.Installer{
		Layout:  eff,
		FS:      install.OSFS{},
		Service: ctrl,
		IsAdmin: install.IsAdmin,
		Log:     newLogger(),
	}
	kept, err := inst.Uninstall(*purge)
	if err != nil {
		return err
	}
	fmt.Printf("filearr-agent service %q uninstalled\n", install.ServiceName)
	if len(kept) > 0 {
		fmt.Printf("kept (use --purge to remove): %s\n", strings.Join(kept, ", "))
	}
	return nil
}

// runService is the thin lifecycle wrapper: service status|start|stop|restart.
func runService(args []string) error {
	fs := newFlagSet("service")
	_ = bindCommonFlags(fs)
	if err := fs.Parse(args); err != nil {
		return err
	}
	action := fs.Arg(0)
	if action == "" {
		return fmt.Errorf("usage: filearr-agent service status|start|stop|restart")
	}

	layout, err := install.ResolveLayout(runtime.GOOS, os.Getenv)
	if err != nil {
		return err
	}
	svcCfg := install.ServiceConfig(layout, activeSidecar().Path, runtime.GOOS)
	ctrl, err := install.NewController(install.NoopProgram{}, svcCfg)
	if err != nil {
		return err
	}

	switch action {
	case "status":
		st, serr := ctrl.Status()
		if serr != nil {
			return serr
		}
		fmt.Printf("filearr-agent service: %s\n", st)
		return nil
	case "start":
		if err := ctrl.Start(); err != nil {
			return err
		}
		fmt.Println("filearr-agent service started")
		agentlog.Verbose(newLogger(), "service start requested")
		return nil
	case "stop":
		if err := ctrl.Stop(); err != nil {
			return err
		}
		fmt.Println("filearr-agent service stopped")
		return nil
	case "restart":
		if err := ctrl.Restart(); err != nil {
			return err
		}
		fmt.Println("filearr-agent service restarted")
		return nil
	default:
		return fmt.Errorf("unknown service action %q (want status|start|stop|restart)", action)
	}
}

// effectiveLayout adjusts the resolved OS layout with the operator's chosen data
// and log directories. A service install defaults data to the SYSTEM layout dir
// (not the per-user default that bindCommonFlags would otherwise pick when
// nothing is configured), because the service runs machine-wide. An explicit
// -data flag, FILEARR_AGENT_DATA_DIR, or a sidecar data_dir overrides it; a
// configured log dir (flag/env/sidecar) overrides the layout log dir.
func effectiveLayout(cfg *config, set map[string]bool, layout install.Layout) install.Layout {
	eff := layout
	if set["data"] || os.Getenv(envDataDir) != "" || activeSidecar().DataDir != "" {
		eff.DataDir = cfg.DataDir
	}
	if cfg.LogDir != "" {
		eff.LogDir = cfg.LogDir
	}
	return eff
}

// flagsSet returns the set of flag names explicitly provided on the command line
// (via flag.FlagSet.Visit), so the resolver can distinguish "operator chose the
// default value" from "value was defaulted".
func flagsSet(fs *flag.FlagSet) map[string]bool {
	set := map[string]bool{}
	fs.Visit(func(f *flag.Flag) { set[f.Name] = true })
	return set
}
