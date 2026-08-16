package install

import (
	"fmt"
	"os"

	"golang.org/x/sys/windows"
)

// RemoveBinaryDeferred handles the one case os.Remove cannot on Windows: the
// binary is a RUNNING image — `filearr-agent uninstall` is invoked from the
// installed exe itself, and/or the just-stopped service process has not fully
// exited — so unlinking it fails with "Access is denied". A running image CAN be
// renamed, so the file is moved aside (out of the install path immediately) and
// its deletion is scheduled for the next reboot via MoveFileEx
// MOVEFILE_DELAY_UNTIL_REBOOT (the same mechanism Windows installers use).
// Returns the parked path so the caller can say so.
func (OSFS) RemoveBinaryDeferred(path string) (parked string, err error) {
	parked = path + ".uninstalled"
	_ = os.Remove(parked) // a leftover from an earlier attempt, if it is free now
	if err := os.Rename(path, parked); err != nil {
		return "", fmt.Errorf("move running binary aside: %w", err)
	}
	// If nothing holds it any more (the service finished exiting), just delete.
	if err := os.Remove(parked); err == nil {
		return "", nil
	}
	src, err := windows.UTF16PtrFromString(parked)
	if err != nil {
		return parked, err
	}
	if err := windows.MoveFileEx(src, nil, windows.MOVEFILE_DELAY_UNTIL_REBOOT); err != nil {
		return parked, fmt.Errorf("schedule delete on reboot: %w", err)
	}
	return parked, nil
}
