package inventory

import (
	"testing"

	"github.com/filearr/filearr/agent/internal/inventory/permissions"
)

// The full-ACE permissions collector is registered + advertised exactly where
// a real read exists (Linux, Windows) and NOT elsewhere (W7, 2026-08-19).
func TestPermissionsAdvertisedIffSupported(t *testing.T) {
	_, ok := DefaultRegistry().Get(permissions.CollectorName)
	if ok != permissions.Supported() {
		t.Fatalf("registered=%v supported=%v", ok, permissions.Supported())
	}
	caps := Capabilities()
	advertised, _ := caps["inventory_collectors"].([]string)
	found := false
	for _, n := range advertised {
		if n == permissions.CollectorName {
			found = true
		}
	}
	if found != permissions.Supported() {
		t.Fatalf("advertised=%v supported=%v (%v)", found, permissions.Supported(), advertised)
	}
}
