package extract

import (
	"archive/tar"
	"archive/zip"
	"compress/bzip2"
	"compress/gzip"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
)

// Archive families, mirroring backend/filearr/tasks/archives.py exactly: the
// same zip extensions and the same tar suffix table, checked in the same order
// (compound ".tar.gz" before a bare ".gz" could match). 7z/rar need third-party
// readers on both sides and are recognised by neither.
var zipArchiveExts = map[string]bool{"zip": true, "cbz": true, "jar": true}

var tarSuffixes = []string{"tar.gz", "tar.bz2", "tar.xz", "tar", "tgz", "tbz2", "tbz", "txz"}

// tarDecompressor returns the stream decoder for a tar suffix.
//
// .tar.xz / .txz are NOT listable here: Go's stdlib has no xz decoder and the
// parity plan forbids new module dependencies for it. They are reported as
// unsupported (no metadata, no error) rather than half-listed — central still
// lists them for centrally-mounted files.
func tarDecompressor(suffix string) (func(io.Reader) (io.Reader, error), bool) {
	switch suffix {
	case "tar":
		return func(r io.Reader) (io.Reader, error) { return r, nil }, true
	case "tar.gz", "tgz":
		return func(r io.Reader) (io.Reader, error) { return gzip.NewReader(r) }, true
	case "tar.bz2", "tbz2", "tbz":
		return func(r io.Reader) (io.Reader, error) { return bzip2.NewReader(r), nil }, true
	}
	return nil, false
}

// archiveFormat is the stored `archive.format` tag: the matched tar suffix
// ("tar.gz") or else the plain extension ("zip"). Port of archives.py
// _archive_format.
func archiveFormat(path string) string {
	name := strings.ToLower(filepath.Base(path))
	for _, suffix := range tarSuffixes {
		if strings.HasSuffix(name, "."+suffix) {
			return suffix
		}
	}
	return lowerExt(path)
}

// accumulator collects the bounded member listing. It is a port of archives.py
// _Accumulator minus the flat search string (central builds `archive_members`
// from `archive.members` in its own projection; the wire contract carries only
// the structured object).
type accumulator struct {
	members   []map[string]any
	count     int
	total     int64
	truncated bool
}

// full reports that the enumeration COUNT cap is reached (stop + mark truncated).
func (a *accumulator) full() bool { return a.count >= ArchiveMaxMembers }

func (a *accumulator) add(name string, size int64) {
	// Member names are UNTRUSTED strings: control-stripped and length-capped for
	// safe storage, but the path STRUCTURE is preserved verbatim ("../evil" stays
	// "../evil"). The name is a display/search value and is NEVER resolved,
	// normalised, or joined to a filesystem path.
	clean := truncateRunes(stripControl(name), MemberNameCap)
	if size < 0 {
		size = 0
	}
	a.count++
	a.total += size
	if len(a.members) < ArchiveMembersStored {
		a.members = append(a.members, map[string]any{"name": clean, "size": size})
	}
}

// listArchive emits the `archive` object for a zip- or tar-family file. An
// unrecognised extension writes nothing and returns nil (the taxonomy's
// "archive" category is broader than what can be listed index-only).
func listArchive(ctx context.Context, path string, opts Options, res *Result) error {
	format := archiveFormat(path)
	acc := &accumulator{members: []map[string]any{}}

	switch {
	case zipArchiveExts[lowerExt(path)]:
		if err := listZip(ctx, path, acc); err != nil {
			return err
		}
	case tarSuffixMatch(path) != "":
		decomp, ok := tarDecompressor(tarSuffixMatch(path))
		if !ok {
			return nil // xz and friends: unsupported, not failed
		}
		if err := listTar(ctx, path, acc, decomp); err != nil {
			return err
		}
	default:
		return nil
	}

	res.Meta["archive"] = map[string]any{
		"member_count":       acc.count,
		"total_uncompressed": acc.total,
		"members":            acc.members,
		"truncated":          acc.truncated,
		"format":             format,
	}
	return nil
}

func tarSuffixMatch(path string) string {
	name := strings.ToLower(filepath.Base(path))
	for _, suffix := range tarSuffixes {
		if strings.HasSuffix(name, "."+suffix) {
			return suffix
		}
	}
	return ""
}

// listZip enumerates a zip's central directory. The bomb guard runs FIRST —
// before a single member is enumerated — reusing the exact discipline the
// document extractors apply.
func listZip(ctx context.Context, path string, acc *accumulator) error {
	zr, err := zip.OpenReader(path)
	if err != nil {
		return fmt.Errorf("not a valid zip archive: %w", err)
	}
	defer zr.Close()
	if err := guardDecompression(&zr.Reader); err != nil {
		return err
	}
	for _, f := range zr.File {
		if err := ctx.Err(); err != nil {
			return err
		}
		if f.FileInfo().IsDir() {
			continue
		}
		if acc.full() {
			acc.truncated = true
			break
		}
		acc.add(f.Name, int64(f.UncompressedSize64))
	}
	return nil
}

// listTar streams tar headers under TWO bounds, because tar has no central
// directory to guard against: a member-count cap and a COMPRESSED-stream byte
// ceiling (ArchiveScanMaxBytes) past which listing stops cleanly and is marked
// truncated. A decompression-bomb tar can therefore never force unbounded work.
// No member payload is ever decompressed to disk.
func listTar(ctx context.Context, path string, acc *accumulator, decomp func(io.Reader) (io.Reader, error)) error {
	f, err := os.Open(path)
	if err != nil {
		return fmt.Errorf("cannot open tar archive: %w", err)
	}
	defer f.Close()

	counter := &countingReader{r: f, limit: ArchiveScanMaxBytes}
	stream, err := decomp(counter)
	if err != nil {
		return fmt.Errorf("not a valid tar archive: %w", err)
	}
	tr := tar.NewReader(stream)
	for {
		if cerr := ctx.Err(); cerr != nil {
			return cerr
		}
		hdr, err := tr.Next()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			// A short/partial stream (typically our own byte ceiling cutting it
			// mid-member) is a CLEAN truncation once we already have members;
			// only a failure before ANY member is a real parse error.
			if acc.count == 0 && !counter.capped {
				return fmt.Errorf("tar listing failed: %w", err)
			}
			acc.truncated = true
			break
		}
		if acc.full() || counter.capped {
			acc.truncated = true
			break
		}
		if hdr.Typeflag != tar.TypeReg {
			continue // skip dirs/symlinks/devices from the member list
		}
		acc.add(hdr.Name, hdr.Size)
	}
	if counter.capped {
		acc.truncated = true
	}
	return nil
}

// countingReader is a forward-only wrapper that stops the underlying read once
// `limit` compressed bytes have been pulled, recording that it capped.
type countingReader struct {
	r      io.Reader
	limit  int64
	count  int64
	capped bool
}

func (c *countingReader) Read(p []byte) (int, error) {
	if c.count >= c.limit {
		c.capped = true
		return 0, io.EOF
	}
	if int64(len(p)) > c.limit-c.count {
		p = p[:c.limit-c.count]
	}
	n, err := c.r.Read(p)
	c.count += int64(n)
	return n, err
}
