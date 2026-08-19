package permissions

import (
	"context"
	"errors"
	"io/fs"
)

// CollectorName is the stable name the (future) real collector advertises and an
// admin composes against. It is intentionally distinct from the existing
// summary-only "perms" collector (brief §0).
const CollectorName = "permissions"

// ErrPermissionsScaffold is the sentinel every inert boundary returns. It makes
// the scaffold's incompleteness LOUD: the collector never yields wrong or empty
// data masquerading as a real, verified "no permissions found" result. The real
// per-OS reads (W7-T2/T3/T4) replace the stubs that return it.
var ErrPermissionsScaffold = errors.New(
	"permissions collector is scaffold-only: per-OS ACL reads not implemented (W7)")

// Collector is the full-ACE permissions collector. It satisfies the parent
// package's inventory.Collector interface structurally (Name + Collect) WITHOUT
// importing it, so registering it later creates no import cycle.
//
// Registered in inventory.DefaultRegistry() since 2026-08-19 (Linux + Windows
// reads real; darwin/other still return ErrPermissionsScaffold per file, which
// the runner records as a per-entry error -- the collector is advertised only
// where it can read: see inventory.DefaultRegistry).
type Collector struct{}

// Supported reports whether this build/OS has a real read behind Collect.
func Supported() bool { return supported }

// Name returns the stable collector identifier.
func (Collector) Name() string { return CollectorName }

// Collect routes through the per-OS read (collectRecord: Linux xattr/mode,
// Windows security descriptor; other platforms return ErrPermissionsScaffold)
// and emits the Record as ONE map entry, "record", so central ingests the exact
// normalized shape (brief §3.1) rather than a re-flattened copy. Errors are
// per-file and fail-soft under the runner's contract.
func (Collector) Collect(_ context.Context, path string, info fs.FileInfo) (map[string]any, error) {
	rec, err := collectRecord(path, info)
	if err != nil {
		return nil, err
	}
	return map[string]any{"record": rec}, nil
}
