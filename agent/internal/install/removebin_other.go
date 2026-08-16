//go:build !windows

package install

// RemoveBinaryDeferred is Windows-only: on Unix an open executable unlinks
// fine, so os.Remove failing is a real error and there is nothing to defer.
func (OSFS) RemoveBinaryDeferred(path string) (string, error) {
	return "", errNoDeferredRemove
}
