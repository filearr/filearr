// Package extract is the agent-side extraction pass (agent-parity design
// contract, 2026-08-09): it reads a file the agent can see and central cannot,
// and produces the flat, CENTRAL-VOCABULARY metadata object that rides the
// replication event as `extracted`. Central applies the result into
// Item.metadata_ (the EXTRACTED column — invariant 2), so parity is reached
// without central ever opening a remote file.
//
// # Why the key names look "Pythonic"
//
// Every key emitted here is the key central's own extractors emit
// (backend/filearr/tasks/{extract,documents,archives,ffprobe}.py). Deliberate:
// the wire contract says `meta` uses central's existing vocabulary so there is
// NO translation layer on either side. When you add a key, copy it from the
// central extractor rather than inventing one.
//
// # Discipline
//
//   - Heavy capabilities are HOST TOOLS, not compiled code (ffprobe, tesseract).
//     Absent tool == absent capability, never an error.
//   - Every extractor is individually fallible: a failure lands in Result.Errors
//     and the rest of the pass still returns what worked. Extractors run under a
//     panic guard — a hostile file must never take down a scan.
//   - Every read is bounded (file size, decompressed bytes, member count, output
//     bytes, characters) and honors ctx cancellation.
package extract

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

// Schema is the `extracted.schema` wire version. Bump it when the MEANING of an
// emitted key changes (central gates on it); purely additive keys do not.
const Schema = 1

// Taxonomy file_category values this pass understands. They are the agent's own
// taxonomy categories (internal/taxonomy seed, mirrored from central's
// file_groups seed), not a second vocabulary.
const (
	CategoryImage    = "image"
	CategoryAudio    = "audio"
	CategoryVideo    = "video"
	CategoryDocument = "document"
	CategoryArchive  = "archive"
)

// builtinCategories are the categories this BUILD can extract with compiled-in
// code alone — no host tool required (stdlib image/zip/tar/xml + the already
// vendored dhowden/tag).
var builtinCategories = []string{CategoryArchive, CategoryAudio, CategoryDocument, CategoryImage}

// Formats reports the categories the agent can actually extract on this host:
// the compiled-in set, plus "video" when ffprobe is present (video carries no
// pure-Go extractor — its whole content is the technical probe). Sorted, so the
// advertised capability object is stable across polls.
func Formats(hasFFprobe bool) []string {
	out := append([]string(nil), builtinCategories...)
	if hasFFprobe {
		out = append(out, CategoryVideo)
	}
	sort.Strings(out)
	return out
}

// Options configures one extraction pass. Zero values are safe: the pass then
// runs property/metadata extraction only (no body text, no OCR, no host tools),
// with central's own caps.
type Options struct {
	// BodyText includes document body text (policy extract_body_text). It makes
	// events materially bigger, which is why it is a separate opt-in.
	BodyText bool
	// OCR runs tesseract over images (policy extract_ocr). Requires TesseractPath.
	OCR bool
	// MaxBytes skips files larger than this (policy extract_max_bytes). <= 0
	// means no size gate.
	MaxBytes int64

	// Host tool paths, already resolved by the caller (internal/inventory). An
	// empty path means "tool absent" — the dependent extractor is skipped, not
	// failed.
	FFprobePath   string
	TesseractPath string

	// MaxBodyChars caps stored body text; 0 => BodyTextMaxChars.
	MaxBodyChars int
	// MaxOCRChars caps stored OCR text; 0 => OCRMaxChars.
	MaxOCRChars int
	// FFprobeTimeout / OCRTimeout bound one subprocess; 0 => the central-mirrored
	// defaults.
	FFprobeTimeout time.Duration
	OCRTimeout     time.Duration
	// OCRLang is the tesseract -l value; "" => OCRLang.
	OCRLang string
}

// withDefaults fills the zero knobs with the central-mirrored constants, so a
// caller only has to express POLICY (enabled/body/ocr/max bytes) and tool paths.
func (o Options) withDefaults() Options {
	if o.MaxBodyChars <= 0 {
		o.MaxBodyChars = BodyTextMaxChars
	}
	if o.MaxOCRChars <= 0 {
		o.MaxOCRChars = OCRMaxChars
	}
	if o.FFprobeTimeout <= 0 {
		o.FFprobeTimeout = FFprobeTimeout
	}
	if o.OCRTimeout <= 0 {
		o.OCRTimeout = OCRTimeout
	}
	if o.OCRLang == "" {
		o.OCRLang = OCRLang
	}
	return o
}

// Result is one file's extraction output. Meta holds the flat central-vocabulary
// keys; BodyText is carried SEPARATELY (not inside Meta) because the wire object
// gives it its own field and its own truncation flag. Errors maps an extractor
// name to a sanitised, length-capped message — a partial success is the normal,
// expected outcome for a mixed library.
type Result struct {
	Meta              map[string]any
	BodyText          string
	BodyTextTruncated bool
	Errors            map[string]string
}

// Empty reports whether the result carries nothing worth shipping. An
// errors-only result is NOT empty: an operator needs to see why a file failed,
// and central records it alongside its own extraction errors.
func (r *Result) Empty() bool {
	return r == nil || (len(r.Meta) == 0 && r.BodyText == "" && len(r.Errors) == 0)
}

// errMessageCap bounds one recorded extractor error. A parser can produce a
// multi-kilobyte message from hostile input; the wire object is size-capped
// server-side, so a runaway message must never be what fills it.
const errMessageCap = 300

// run executes one extractor under a panic guard and records a per-extractor
// failure instead of propagating it. The panic guard is not paranoia: these
// parsers consume untrusted bytes, and a panic here would kill an entire scan
// (the pass runs inline in the walk).
func (r *Result) run(name string, fn func() error) {
	err := func() (err error) {
		defer func() {
			if p := recover(); p != nil {
				err = fmt.Errorf("panic: %v", p)
			}
		}()
		return fn()
	}()
	if err == nil {
		return
	}
	if r.Errors == nil {
		r.Errors = map[string]string{}
	}
	r.Errors[name] = truncateRunes(stripControl(err.Error()), errMessageCap)
}

// set stores a non-empty string value under key (central omits empty values
// rather than storing "").
func (r *Result) set(key, value string) {
	if v := cleanStr(value); v != "" {
		r.Meta[key] = v
	}
}

// Extract runs the pass for one file. category is the taxonomy file_category the
// scan already computed (image/audio/video/document/archive/…), so extraction
// never re-classifies.
//
// Returns (nil, nil) — a skip, not a failure — when: the category has no
// extractor here, the path is not a regular file, or the file exceeds
// opts.MaxBytes. Returns an error ONLY for a framing failure (cannot stat,
// context cancelled); everything an extractor can hit is recorded in
// Result.Errors instead.
func Extract(ctx context.Context, path, category string, opts Options) (*Result, error) {
	opts = opts.withDefaults()

	info, err := os.Stat(path)
	if err != nil {
		return nil, fmt.Errorf("extract: stat %s: %w", path, err)
	}
	if !info.Mode().IsRegular() {
		return nil, nil // a device/fifo/symlink loop is never extracted
	}
	if opts.MaxBytes > 0 && info.Size() > opts.MaxBytes {
		return nil, nil // policy extract_max_bytes — a skip, deliberately silent
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}

	res := &Result{Meta: map[string]any{}}
	ext := lowerExt(path)

	switch category {
	case CategoryImage:
		res.run("image", func() error { return extractImage(path, res) })
		if opts.OCR && opts.TesseractPath != "" {
			res.run("ocr", func() error { return ocrImage(ctx, path, opts, res) })
		}
	case CategoryAudio:
		res.run("audio", func() error { return extractAudioTags(path, res) })
		// dhowden/tag reads TAGS, never the stream, so duration/bitrate/
		// samplerate/channels — which central gets from tinytag — only exist
		// here when ffprobe does. Absent ffprobe, the audio item still carries
		// its tags; it just lacks the technical fields.
		if opts.FFprobePath != "" {
			res.run("ffprobe", func() error { return probeAudio(ctx, path, opts, res) })
		}
	case CategoryVideo:
		if opts.FFprobePath == "" {
			return nil, nil // no compiled-in video extractor; the probe IS the extractor
		}
		res.run("ffprobe", func() error { return probeVideo(ctx, path, opts, res) })
	case CategoryDocument:
		res.run("document", func() error { return extractDocument(ctx, path, ext, opts, res) })
	case CategoryArchive:
		res.run("archive", func() error { return listArchive(ctx, path, opts, res) })
	default:
		return nil, nil
	}

	if res.Empty() {
		return nil, nil
	}
	return res, nil
}

// lowerExt returns the lower-cased extension without its dot ("" when none).
func lowerExt(path string) string {
	return strings.ToLower(strings.TrimPrefix(filepath.Ext(path), "."))
}
