//go:build windows

package extract

import (
	"bufio"
	"errors"
	"io"
	"os"
	"strings"
)

// readProvenance reads the Zone.Identifier alternate data stream that Windows
// (Edge/Chrome/Firefox, the shell's Mark-of-the-Web) attaches to downloaded
// files:
//
//	[ZoneTransfer]
//	ZoneId=3
//	ReferrerUrl=https://example.com/page
//	HostUrl=https://cdn.example.com/file.zip
//
// An absent stream is the normal case (not an error). The stream is tiny; read
// is capped defensively.
func readProvenance(path string, res *Result) error {
	f, err := os.Open(path + ":Zone.Identifier")
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil
		}
		// Access denied / not NTFS / sharing violation: provenance is optional.
		return nil
	}
	defer f.Close()
	var host, ref string
	sc := bufio.NewScanner(io.LimitReader(f, 16*1024))
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if i := strings.IndexByte(line, '='); i > 0 {
			k, v := strings.ToLower(strings.TrimSpace(line[:i])), strings.TrimSpace(line[i+1:])
			switch k {
			case "hosturl":
				host = v
			case "referrerurl":
				ref = v
			}
		}
	}
	setProvenance(res, host, ref)
	return nil
}
