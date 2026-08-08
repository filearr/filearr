package inventory

import "testing"

// The env override is the authoritative signal (the shipped agent image sets
// FILEARR_AGENT_CONTAINER=1): any value except "0"/"false" means container,
// and those two explicitly opt back OUT even where /.dockerenv exists (the
// documented escape hatch for deliberate in-container self-update swaps).
func TestInContainerEnvSemantics(t *testing.T) {
	cases := []struct {
		val  string
		want bool
	}{
		{"1", true},
		{"true", true},
		{"yes", true}, // any non-opt-out value counts
		{"0", false},
		{"false", false},
	}
	for _, tc := range cases {
		t.Setenv("FILEARR_AGENT_CONTAINER", tc.val)
		if got := InContainer(); got != tc.want {
			t.Errorf("InContainer() with env %q = %v, want %v", tc.val, got, tc.want)
		}
	}
}

// The capability advertisement must carry the container fact — central's
// flag-but-never-offer behavior keys off the stored `container` capability.
func TestCapabilitiesAdvertiseContainer(t *testing.T) {
	t.Setenv("FILEARR_AGENT_CONTAINER", "1")
	caps := Capabilities()
	if caps["container"] != true {
		t.Fatalf("capabilities missing container=true: %v", caps)
	}
	t.Setenv("FILEARR_AGENT_CONTAINER", "0")
	caps = Capabilities()
	if caps["container"] != false {
		t.Fatalf("expected container=false with opt-out env: %v", caps)
	}
}
