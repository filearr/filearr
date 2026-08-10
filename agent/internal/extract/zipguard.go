package extract

import (
	"archive/zip"
	"fmt"
	"io"
)

// guardDecompression rejects a decompression-bomb zip container BEFORE any
// member is read, by summing the CENTRAL DIRECTORY's declared uncompressed and
// compressed sizes. It is a port of backend/filearr/tasks/documents.py
// guard_decompression, with the same two gates:
//
//   - total declared uncompressed > DecompressedMax → reject;
//   - overall ratio > DecompressionRatio, but ONLY once the payload already
//     exceeds DecompressionRatioMinBytes — so an ordinary few-KB office file that
//     legitimately inflates 100x is never falsely rejected, and only a genuine
//     bomb is.
//
// Nothing is decompressed here, so the check is cheap and provably happens
// before any parse.
func guardDecompression(zr *zip.Reader) error {
	var totalUncompressed, totalCompressed uint64
	for _, f := range zr.File {
		totalUncompressed += f.UncompressedSize64
		totalCompressed += f.CompressedSize64
	}
	if totalUncompressed > uint64(DecompressedMax) {
		return fmt.Errorf(
			"decompression guard: declared uncompressed size %d exceeds ceiling %d bytes",
			totalUncompressed, DecompressedMax)
	}
	if totalCompressed > 0 {
		ratio := float64(totalUncompressed) / float64(totalCompressed)
		if ratio > DecompressionRatio && totalUncompressed > uint64(DecompressionRatioMinBytes) {
			return fmt.Errorf(
				"decompression guard: compression ratio %.1f:1 exceeds %.0f:1 at %d uncompressed bytes",
				ratio, DecompressionRatio, totalUncompressed)
		}
	}
	return nil
}

// openMember opens the named zip member for STREAMING under a hard byte
// ceiling. The central-directory guard above checks DECLARED sizes and a crafted
// member can lie about its own, so every actual read is additionally bounded by
// MemberReadMax — declared-size checks alone have never been sufficient against
// a zip bomb.
//
// Returns (nil, false) when the member is absent: an optional part (docProps/
// core.xml is optional in OOXML) is a fact, not a failure.
func openMember(zr *zip.Reader, name string) (io.ReadCloser, bool) {
	for _, f := range zr.File {
		if f.Name != name {
			continue
		}
		rc, err := f.Open()
		if err != nil {
			return nil, false
		}
		return &limitedMember{rc: rc, r: io.LimitReader(rc, MemberReadMax)}, true
	}
	return nil, false
}

// limitedMember is a member reader capped at MemberReadMax decompressed bytes.
type limitedMember struct {
	rc io.ReadCloser
	r  io.Reader
}

func (m *limitedMember) Read(p []byte) (int, error) { return m.r.Read(p) }
func (m *limitedMember) Close() error               { return m.rc.Close() }
