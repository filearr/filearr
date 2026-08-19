package extract

import (
	"net/url"
	"strings"
)

// Provenance keys — central's vocabulary (backend/filearr/file_origin.py emits
// the same two from Linux/macOS xattrs on centrally-scanned mounts).
const (
	keyOriginURL     = "origin_url"
	keyReferrerURL   = "referrer_url"
	provenanceURLMax = 2048
)

// cleanProvenanceURL mirrors central's file_origin.clean_url: control bytes
// stripped, length-capped, http(s)/ftp(s)/sftp with a host only. A file: or
// javascript: "origin" is never provenance anyone wants rendered as a link.
func cleanProvenanceURL(raw string) string {
	s := strings.TrimSpace(stripControl(raw))
	if s == "" {
		return ""
	}
	if len(s) > provenanceURLMax {
		s = truncateRunes(s, provenanceURLMax)
	}
	u, err := url.Parse(s)
	if err != nil || u.Host == "" {
		return ""
	}
	switch strings.ToLower(u.Scheme) {
	case "http", "https", "ftp", "ftps", "sftp":
		return s
	}
	return ""
}

// setProvenance stores origin/referrer, dropping a referrer equal to the origin.
func setProvenance(res *Result, origin, referrer string) {
	origin = cleanProvenanceURL(origin)
	referrer = cleanProvenanceURL(referrer)
	if origin != "" {
		res.set(keyOriginURL, origin)
	}
	if referrer != "" && referrer != origin {
		res.set(keyReferrerURL, referrer)
	}
}
