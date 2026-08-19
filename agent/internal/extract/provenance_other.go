//go:build !windows && !linux && !darwin

package extract

// readProvenance: no provenance source on this platform.
func readProvenance(path string, res *Result) error { return nil }
