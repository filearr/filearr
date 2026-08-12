package inventory

import (
	"fmt"

	"golang.org/x/sys/windows"
)

// osVersion reads the real Windows version through RtlGetVersion.
//
// RtlGetVersion rather than GetVersionEx or `cmd /c ver`, for two reasons that
// both matter here:
//
//   - GetVersionEx is subject to the application-compatibility shim: an
//     executable without the right manifest GUIDs is told "6.2" (Windows 8) no
//     matter what it is running on. Reporting Windows 8 from a Server 2025 box
//     would be worse than reporting nothing.
//   - `cmd /c ver` is a subprocess. This is on the command-poll path, and the
//     agent runs as a service on hosts where spawning a shell is exactly the
//     thing security tooling watches; a documented ntdll call is cheaper and
//     quieter.
//
// The BUILD NUMBER is the part worth reading. Windows 11 still reports major
// 10, minor 0 — the marketing name is not in the version at all — so 10.0 with
// build 22000+ is Windows 11 and build 19045 is Windows 10 22H2. The string is
// deliberately not translated into a marketing name: that mapping changes with
// every release and a wrong friendly name is worse than an exact number.
func osVersion() string {
	v := windows.RtlGetVersion()
	if v == nil {
		return ""
	}
	return fmt.Sprintf("Windows %d.%d (build %d)", v.MajorVersion, v.MinorVersion, v.BuildNumber)
}
