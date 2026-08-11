package config

// Local self-administration state (2026-08-10): the settings an OPERATOR ON THE
// MACHINE authored through the agent's local web UI, persisted beside the cached
// central policy.
//
// It is a SEPARATE file from policy.json on purpose. policy.json is central's
// document and is overwritten wholesale on every 200 from the policy poll; a
// local edit written into it would be erased by the next successful poll. It is
// also separate from scan.json, which is the scan PROCESS's configuration and is
// rewritten by `filearr-agent scan -root …`.
//
// Precedence (documented in docs-site/reference/agent-settings.md):
//
//	central policy  >  local override (this file)  >  FILEARR_AGENT_* env
//	                >  sidecar  >  built-in default
//
// Local sits UNDER central by design: a key central explicitly set is re-applied
// on every poll, so a local edit to the same key would silently revert within a
// poll interval. The local UI therefore refuses to edit a centrally-set key
// rather than accepting an edit that will not survive (see LocalSurface.
// CentralKeys and the localapi control handlers).

import (
	"encoding/json"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"time"
)

// LocalSettingsName is the local-override document under the agent data dir.
const LocalSettingsName = "local-settings.json"

// LocalSettings is the on-disk local-override record. Every schedule field is a
// POINTER so "the operator set this locally" is distinguishable from "absent —
// fall through to env/sidecar/default", exactly like the policy view's optional
// keys. Roots are NOT stored here (they live in scan.json, which the scan
// process reads); only the fact that they were edited locally is, so the health
// snapshot can tell central an operator has been changing this agent's setup.
type LocalSettings struct {
	// ScanPaused is the LOCAL scan pause. It is a scan-only flag and is
	// deliberately NOT central's `suspend` (which pauses scanning AND the
	// replication push fleet-wide). Both gate the scheduler; scanning runs only
	// when NEITHER is set, and clearing this one never clears central's.
	ScanPaused bool `json:"scan_paused,omitempty"`

	ScanCron            *string `json:"scan_cron,omitempty"`
	ScanIntervalSeconds *int    `json:"scan_interval_seconds,omitempty"`
	ScanOnStart         *bool   `json:"scan_on_start,omitempty"`

	// ShareMappings are locally-authored network-share locations, keyed by the
	// LOCAL absolute path each covers, valued with the location a remote client
	// opens (smb://host/share[/sub], \\host\share[\sub], nfs://host/export).
	// They are the editable half of the share map: FILEARR_AGENT_SHARE_MAP is
	// the machine's own configuration and cannot be rewritten from a web page,
	// so an operator with local_roots_control fills in the paths the environment
	// does NOT mention (see staticShareMappings in cmd/filearr-agent for the
	// precedence rule and why it is that way round).
	//
	// A map, not a list: one local path can have exactly one location, and the
	// key form makes that structurally true instead of a validation rule. Values
	// are validated with shares.ValidateLocation before they are written, but a
	// hand-edited file may still contain a malformed one — the resolver skips it
	// (R1: hints are best-effort) and the local UI shows it as rejected.
	ShareMappings map[string]string `json:"share_mappings,omitempty"`

	// RootsEditedAt stamps the last local scan-root add/remove (RFC3339 UTC).
	RootsEditedAt string `json:"roots_edited_at,omitempty"`
	// UpdatedAt stamps the last write of any kind (RFC3339 UTC).
	UpdatedAt string `json:"updated_at,omitempty"`
}

// HasOverrides reports whether any SCHEDULE knob is overridden locally (the
// pause flag and the roots stamp are reported separately — they are state and
// provenance, not precedence participants). Share mappings are excluded too:
// they have no central key to be overridden against, so they are host
// configuration this file happens to store, not an override of a policy value.
func (s LocalSettings) HasOverrides() bool {
	return s.ScanCron != nil || s.ScanIntervalSeconds != nil || s.ScanOnStart != nil
}

// LoadLocalSettings reads the local-override document. A missing file is the
// normal state and yields a zero value with a nil error. A CORRUPT file also
// yields a zero value with a nil error: this file gates nothing security
// relevant, and failing a daemon start (or a scan) because an operator's editor
// truncated it would be a far worse outcome than falling back to "no local
// overrides". err is non-nil only for a real I/O failure the caller may want to
// log.
func LoadLocalSettings(dataDir string) (LocalSettings, error) {
	buf, err := os.ReadFile(filepath.Join(dataDir, LocalSettingsName))
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return LocalSettings{}, nil
		}
		return LocalSettings{}, err
	}
	var s LocalSettings
	if json.Unmarshal(buf, &s) != nil {
		return LocalSettings{}, nil // corrupt => no overrides, never fatal
	}
	return s, nil
}

// SaveLocalSettings atomically persists the local-override document (temp file +
// rename, the same durability pattern as the policy cache: a concurrently
// reading scan process never observes a half-written file).
func SaveLocalSettings(dataDir string, s LocalSettings) error {
	s.UpdatedAt = time.Now().UTC().Format(time.RFC3339)
	if err := os.MkdirAll(dataDir, 0o700); err != nil {
		return err
	}
	buf, err := json.MarshalIndent(s, "", "  ")
	if err != nil {
		return err
	}
	return atomicWrite(filepath.Join(dataDir, LocalSettingsName), append(buf, '\n'), 0o644)
}

// UpdateLocalSettings applies mutate to the current document and persists the
// result. Read-modify-write is safe here because every writer is the single
// daemon process serving the local web UI.
func UpdateLocalSettings(dataDir string, mutate func(*LocalSettings)) (LocalSettings, error) {
	s, err := LoadLocalSettings(dataDir)
	if err != nil {
		return LocalSettings{}, err
	}
	mutate(&s)
	if err := SaveLocalSettings(dataDir, s); err != nil {
		return LocalSettings{}, err
	}
	return s, nil
}
