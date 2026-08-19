//go:build linux || darwin

package extract

import (
	"bytes"
	"errors"

	"golang.org/x/sys/unix"
)

const (
	xattrXDGOrigin   = "user.xdg.origin.url"
	xattrXDGReferrer = "user.xdg.referrer.url"
	xattrWhereFroms  = "com.apple.metadata:kMDItemWhereFroms"
)

// readProvenance reads the freedesktop download xattrs (Linux: Firefox/Chrome/
// wget/curl --xattr) or macOS's kMDItemWhereFroms. Filesystems without user
// xattrs (cifs without user_xattr, FAT) answer ENOTSUP — treated as "none".
func readProvenance(path string, res *Result) error {
	get := func(name string) string {
		buf := make([]byte, 4096)
		n, err := unix.Getxattr(path, name, buf)
		if err != nil {
			if errors.Is(err, unix.ERANGE) {
				// larger than 4 KiB: not a URL we want anyway
				return ""
			}
			return "" // ENODATA / ENOATTR / ENOTSUP — absent
		}
		return string(bytes.TrimRight(buf[:n], "\x00"))
	}
	origin := get(xattrXDGOrigin)
	referrer := get(xattrXDGReferrer)
	if origin == "" && referrer == "" {
		if raw := get(xattrWhereFroms); raw != "" {
			origin, referrer = decodeWhereFroms([]byte(raw))
		}
	}
	setProvenance(res, origin, referrer)
	return nil
}
