package main

import (
	"fmt"
	"log/slog"
	"os"
	"strconv"
	"strings"
	"sync"

	"github.com/filearr/filearr/agent/internal/agentlog"
	"github.com/filearr/filearr/agent/internal/sidecar"
)

// The agent resolves ONE sidecar + ONE process logger per invocation, up front
// in the command dispatch wrapper (setupRuntime), so every command shares the
// same configured level/file sink and the same lowest-precedence sidecar
// fallback. Both are held as package state because a CLI process runs exactly
// one command; the mutex only guards the lazy default for direct newLogger()
// callers in tests that never run setupRuntime.
var (
	runtimeMu    sync.Mutex
	activeLogger *slog.Logger
	loadedConfig *sidecar.Config
	logCloser    interface{ Close() error }
	resolvedLogDir string
)

// envLogStderr forces the stderr echo alongside an active file sink even when
// stderr is not a terminal. Set in the container image: its stderr is the
// `docker logs` stream, which must keep carrying every line when the shared
// log dir is enabled.
const envLogStderr = "FILEARR_AGENT_LOG_STDERR"

// activeLogDir returns the log dir resolved by setupRuntime ("" = stderr-only,
// no file sink). The web UI uses it to serve the cross-process merged log.
func activeLogDir() string {
	runtimeMu.Lock()
	defer runtimeMu.Unlock()
	return resolvedLogDir
}

// logFileNameFor gives each COMMAND its own file under the log dir: lumberjack
// rotation is not multi-process safe on a shared path, and a containerized
// agent runs the daemon and scans as separate concurrent processes. The daemon
// keeps the canonical agentlog.LogFileName; everything else is suffixed.
func logFileNameFor(command string) string {
	if command == "run" || command == "" {
		return agentlog.LogFileName
	}
	base := strings.TrimSuffix(agentlog.LogFileName, ".log")
	return base + "-" + command + ".log"
}

// activeSidecar returns the sidecar resolved by setupRuntime, or an empty config
// when none has been loaded (direct-call tests, or a run with no sidecar).
func activeSidecar() *sidecar.Config {
	runtimeMu.Lock()
	defer runtimeMu.Unlock()
	if loadedConfig == nil {
		loadedConfig = &sidecar.Config{}
	}
	return loadedConfig
}

// newLogger returns the process logger configured by setupRuntime. Before
// setupRuntime runs (or in tests) it lazily yields the historic default: an
// info-level text logger on stderr.
func newLogger() *slog.Logger {
	runtimeMu.Lock()
	defer runtimeMu.Unlock()
	if activeLogger == nil {
		activeLogger = slog.New(slog.NewTextHandler(os.Stderr, &slog.HandlerOptions{Level: slog.LevelInfo, ReplaceAttr: nil}))
	}
	return activeLogger
}

// setupRuntime resolves the sidecar and configures the process logger for a
// command, applying the precedence explicit-flag > env > sidecar > default to
// the log level + log dir. It is best-effort: a sidecar load failure or a bad
// level name is reported to stderr and downgraded to defaults rather than
// aborting the command (the command's own preconditions decide fatality). It is
// safe to call more than once; the previous file sink (if any) is closed.
func setupRuntime(command string, args []string) {
	// (a) resolve the sidecar. --config on the command line wins over the env,
	// which DefaultResolver applies when the flag is absent.
	explicit, _ := scanFlagValue(args, "config")
	sc, err := sidecar.DefaultResolver(explicit).Load()
	if err != nil {
		fmt.Fprintf(os.Stderr, "filearr-agent: sidecar config: %v (continuing with env/defaults)\n", err)
		sc = &sidecar.Config{}
	}

	// (b) resolve the log level with the documented precedence.
	levelName := firstNonEmpty(
		flagOrEnv(args, "log-level", envLogLevel),
		sc.LogLevel,
		"info",
	)
	level, ok := agentlog.ParseLevel(levelName)
	if !ok {
		fmt.Fprintf(os.Stderr, "filearr-agent: unknown log level %q; using info\n", levelName)
		level = slog.LevelInfo
	}

	// (c) resolve the log dir (empty => stderr only).
	logDir := firstNonEmpty(
		flagOrEnv(args, "log-dir", envLogDir),
		sc.LogDir,
	)

	forceStderr, _ := strconv.ParseBool(os.Getenv(envLogStderr))
	opts := agentlog.Options{
		Level: level, LogDir: logDir, Stderr: true,
		FileName: logFileNameFor(command), ForceStderr: forceStderr,
	}
	logger, closer, lerr := agentlog.New(opts)
	if lerr != nil {
		fmt.Fprintf(os.Stderr, "filearr-agent: file logging disabled: %v\n", lerr)
		logger, closer, _ = agentlog.New(agentlog.Options{Level: level, Stderr: true})
		logDir = ""
	}

	runtimeMu.Lock()
	if logCloser != nil {
		_ = logCloser.Close()
	}
	activeLogger = logger
	logCloser = closer
	loadedConfig = sc
	resolvedLogDir = logDir
	runtimeMu.Unlock()

	agentlog.Verbose(logger, "runtime configured",
		"command", command, "log_level", levelName, "log_dir", logDir, "sidecar", sc.Path)
}

// flagOrEnv returns the command-line value for -flag/--flag if present, else the
// environment value for envKey, else "". This encodes flag > env for the
// runtime settings that are resolved before the per-command flag.FlagSet parses.
func flagOrEnv(args []string, flagName, envKey string) string {
	if v, ok := scanFlagValue(args, flagName); ok {
		return v
	}
	return os.Getenv(envKey)
}

// scanFlagValue extracts a string flag's value from a raw arg slice, accepting
// -flag value, --flag value, -flag=value, and --flag=value. It is a pre-parse
// peek (the per-command flag.FlagSet still parses authoritatively afterwards);
// only used for the runtime settings that must be known before that parse.
func scanFlagValue(args []string, name string) (string, bool) {
	single, double := "-"+name, "--"+name
	for i := 0; i < len(args); i++ {
		a := args[i]
		switch {
		case a == single || a == double:
			if i+1 < len(args) {
				return args[i+1], true
			}
			return "", true
		case strings.HasPrefix(a, single+"="):
			return strings.TrimPrefix(a, single+"="), true
		case strings.HasPrefix(a, double+"="):
			return strings.TrimPrefix(a, double+"="), true
		}
	}
	return "", false
}
