package extract

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"math"
	"os/exec"
	"strconv"
	"strings"
	"time"
)

// Ceilings for the exiftool pass. They live here rather than in caps.go because
// they are the EXIF story's own numbers and only this file consumes them; the
// mirrored-ceiling convention (cite the central setting, never re-tune) still
// applies.
const (
	// exifTimeout mirrors backend/filearr/config.py Settings.exif_timeout_s
	// (= 30.0), the wall clock after which the exiftool child is killed.
	exifTimeout = 30 * time.Second

	// exifMaxOutputBytes mirrors config.py Settings.exif_max_output_bytes
	// (= 8_388_608). Central compares `len(stdout) > max_output_bytes` and RAISES
	// — an over-cap output is a hard failure, never a silent truncation, because a
	// half-read tag dict would produce metadata that is wrong rather than absent.
	exifMaxOutputBytes = 8_388_608

	// exifStrCap mirrors backend/filearr/exif.py _STR_CAP (= 500). Python slices
	// str by CODE POINTS, so the agent counts runes (truncateRunes), not bytes.
	exifStrCap = 500
)

// exifField is one row of backend/filearr/exif.py _EXIF_FIELD_MAP: the exiftool
// tag, the curated `exif.*` target key, and whether the value is coerced as a
// number or as a string.
type exifField struct {
	tag string
	key string
	num bool
}

// exifFieldMap is a row-for-row, ORDER-PRESERVING copy of _EXIF_FIELD_MAP. Order
// is load-bearing for exactly one pair: DateTimeOriginal precedes CreateDate and
// both target exif.taken_at, so central's `out.setdefault(target, val)` makes the
// first non-empty value win. Reordering this slice would silently change which
// timestamp an item carries.
var exifFieldMap = []exifField{
	{tag: "Make", key: "exif.camera_make"},
	{tag: "Model", key: "exif.camera_model"},
	{tag: "LensModel", key: "exif.lens_model"},
	{tag: "LensID", key: "exif.lens_id"},
	{tag: "ISO", key: "exif.iso", num: true},
	{tag: "ExposureTime", key: "exif.exposure_time", num: true},
	{tag: "FNumber", key: "exif.f_number", num: true},
	{tag: "FocalLength", key: "exif.focal_length", num: true},
	{tag: "ImageWidth", key: "exif.width", num: true},
	{tag: "ImageHeight", key: "exif.height", num: true},
	{tag: "DateTimeOriginal", key: "exif.taken_at"},
	{tag: "CreateDate", key: "exif.taken_at"}, // fallback when DateTimeOriginal absent
	{tag: "GPSLatitude", key: "exif.gps_latitude", num: true},
	{tag: "GPSLongitude", key: "exif.gps_longitude", num: true},
	{tag: "GPSAltitude", key: "exif.gps_altitude", num: true},
}

// extractEXIF runs ONE exiftool subprocess and merges central's curated `exif.*`
// keys into the result.
//
// It NEVER returns a tool-level failure. Central's tasks/exif_run.py
// exif_metadata catches every ExifError and degrades to
// `{"_exif_error": sanitize_error(exc)}` so the supplementary EXIF pass can never
// fail the whole extract — and the agent's caller (Result.run) would turn any
// returned error into an entry central reads as a failed extraction. So a
// timeout, a non-zero exit, an over-cap output or unparseable JSON all land in
// `_exif_error` and return nil; only a genuine panic surfaces as a failure, via
// the caller's guard.
func extractEXIF(ctx context.Context, path string, opts Options, res *Result) error {
	raw, err := runExiftool(ctx, opts.ExiftoolPath, path)
	if err != nil {
		res.Meta["_exif_error"] = truncateRunes(cleanStr(err.Error()), exifStrCap)
		return nil
	}
	mapExifTags(raw, res)
	return nil
}

// runExiftool executes exiftool against path and returns the decoded tag object.
//
// The argv, the timeout and the output cap mirror backend/filearr/exif.py
// run_exiftool exactly: `exiftool -json -n -charset filename=utf8 <path>` as an
// argv LIST (never a shell string, so nothing in the untrusted path is
// interpreted), a hard 30s deadline that kills the child, and an 8 MiB stdout
// ceiling. `-n` is what makes GPS come back as signed decimals and numeric tags
// as numbers instead of exiftool's human-formatted strings, so dropping it would
// change every numeric value both sides store.
//
// Central passes no `--` option terminator and neither do we: exiftool has no
// such terminator, and the paths reaching this function come from the scanner's
// own walk (always absolute), never from user text.
func runExiftool(ctx context.Context, binary, path string) (map[string]any, error) {
	cctx, cancel := context.WithTimeout(ctx, exifTimeout)
	defer cancel()

	argv := []string{"-json", "-n", "-charset", "filename=utf8", path}
	cmd := exec.CommandContext(cctx, binary, argv...)
	// Bounded stdout: the capped buffer stops accumulating at the ceiling instead
	// of letting a pathological tag dump balloon the scan process's heap. It
	// overflows only PAST the limit, so an output of exactly 8 MiB is accepted —
	// the same boundary as central's `>` comparison.
	out := &capBuffer{limit: exifMaxOutputBytes}
	var stderr bytes.Buffer
	cmd.Stdout = out
	cmd.Stderr = &stderr

	if err := cmd.Run(); err != nil {
		if cctx.Err() == context.DeadlineExceeded {
			return nil, fmt.Errorf("exiftool timed out after %s", exifTimeout)
		}
		if msg := lastLine(stderr.String()); msg != "" {
			return nil, fmt.Errorf("exiftool failed: %s", msg)
		}
		return nil, fmt.Errorf("exiftool failed: %w", err)
	}
	if out.overflowed {
		return nil, fmt.Errorf("exiftool output too large (> %d bytes)", exifMaxOutputBytes)
	}
	return decodeExiftoolJSON(out.buf.Bytes())
}

// decodeExiftoolJSON unwraps exiftool's `-json` output. It emits a LIST with one
// object per input file, and we always pass exactly one file — but central
// tolerates a bare object too (`if isinstance(data, list): data = data[0] …`),
// and matching that tolerance costs nothing.
//
// UseNumber keeps integers integral: an int tag decoded through float64 and back
// would store `exif.iso` as 100.0 where central stores 100, which is a visible
// parity difference in the API payload.
func decodeExiftoolJSON(b []byte) (map[string]any, error) {
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.UseNumber()
	var doc any
	if err := dec.Decode(&doc); err != nil {
		return nil, fmt.Errorf("exiftool output not valid JSON: %w", err)
	}
	switch t := doc.(type) {
	case []any:
		if len(t) == 0 {
			return map[string]any{}, nil // central's `data[0] if data else {}`
		}
		if m, ok := t[0].(map[string]any); ok {
			return m, nil
		}
	case map[string]any:
		return t, nil
	}
	return nil, fmt.Errorf("exiftool output was not a JSON object")
}

// mapExifTags projects a raw exiftool tag object onto the curated `exif.*`
// namespace and the two flat image keys. It is a behavioural port of
// backend/filearr/exif.py map_exif_tags: pure, defensively typed (this is
// untrusted parser output), unknown or odd values dropped rather than raised,
// and a tag whose target is already populated does NOT overwrite it.
//
// GPS values are stored RAW here, exactly as central does. The exposure decision
// lives entirely in central's `strip_gps` gate plus the per-library `expose_gps`
// flag, and NEVER in an extractor. That split is the whole point of the control:
// an extractor that pre-filtered would make `metadata_` (the source of truth,
// invariant 2) lossy, and one that decided for itself would put a CWE-1230
// judgement in the wrong layer. This is a security invariant, not a style
// choice — do not "helpfully" drop GPS here.
func mapExifTags(raw map[string]any, res *Result) {
	for _, f := range exifFieldMap {
		v, present := raw[f.tag]
		if !present {
			continue
		}
		if _, filled := res.Meta[f.key]; filled {
			continue // setdefault: first non-empty wins (CreateDate never displaces DateTimeOriginal)
		}
		if f.num {
			if n, ok := exifNum(v); ok {
				res.Meta[f.key] = n
			}
			continue
		}
		if s := exifStr(v); s != "" {
			res.Meta[f.key] = s
		}
	}
	setFlatImageEXIF(raw, res)
}

// setFlatImageEXIF fills the two FLAT keys central's Pillow pass produces
// (tasks/extract.py extract_image). The agent's own image pass cannot: it reads
// headers with image.DecodeConfig, which does not look at the EXIF IFD at all
// (image.go says so), so without exiftool an agent-scanned photo would simply be
// missing `camera`/`taken_at` next to a centrally-scanned one.
//
// Both keys are written only when absent, so a value some other pass already
// established wins.
func setFlatImageEXIF(raw map[string]any, res *Result) {
	if _, filled := res.Meta["camera"]; !filled {
		// central: f"{exif.get(271, '')} {exif.get(272, '')}".strip() — EXIF tags
		// 271/272 are Make/Model, so a Make-only or Model-only file yields the one
		// token it has. Re-capped because two 500-rune tags concatenate to 1001.
		camera := strings.TrimSpace(exifStr(raw["Make"]) + " " + exifStr(raw["Model"]))
		res.set("camera", truncateRunes(camera, exifStrCap))
	}
	if _, filled := res.Meta["taken_at"]; !filled {
		// SUBTLE, and please do not "fix" it: central's flat taken_at comes from
		// EXIF tag 306, which is DateTime (the file's last-modified-by-camera
		// stamp) and whose exiftool name is ModifyDate — NOT DateTimeOriginal.
		// DateTimeOriginal has its own curated key (exif.taken_at) above, and the
		// two genuinely differ on any edited photo. Reading DateTimeOriginal here
		// would make the agent's flat key disagree with central's for every such
		// file.
		res.set("taken_at", exifStr(raw["ModifyDate"]))
	}
}

// exifNum is a port of exif.py _num: booleans are rejected (Python guards
// `isinstance(v, bool)` before the int/float check, because True would otherwise
// coerce to 1), real numbers pass through with their integer-ness preserved,
// numeric STRINGS become floats (Python's float("100") is 100.0, not 100), and
// NaN is dropped. Infinities are dropped too — central would keep them, but they
// are not representable in JSON and would cost us the entire event rather than
// one tag.
func exifNum(v any) (any, bool) {
	switch t := v.(type) {
	case bool:
		return nil, false
	case json.Number:
		if i, err := t.Int64(); err == nil {
			return i, true
		}
		return finiteFloat(t.String())
	case int:
		return int64(t), true
	case int64:
		return t, true
	case float64:
		if math.IsNaN(t) || math.IsInf(t, 0) {
			return nil, false
		}
		return t, true
	case string:
		return finiteFloat(t)
	}
	return nil, false
}

// finiteFloat parses s as a float and rejects NaN/±Inf (including the "nan" and
// "inf" spellings ParseFloat accepts, which Python's float() also accepts and
// _num then drops).
func finiteFloat(s string) (any, bool) {
	f, err := strconv.ParseFloat(strings.TrimSpace(s), 64)
	if err != nil || math.IsNaN(f) || math.IsInf(f, 0) {
		return nil, false
	}
	return f, true
}

// exifStr is a port of exif.py _str (`str(v).strip()[:_STR_CAP]`), plus the
// control-stripping every other string value in this package gets: these strings
// reach the search index and the UI, and an EXIF tag is attacker-controlled
// bytes. Anything normalising to empty yields "" so the caller omits the key
// rather than storing "".
func exifStr(v any) string {
	var s string
	switch t := v.(type) {
	case nil:
		return "" // central's _str returns None for None
	case string:
		s = t
	case json.Number:
		s = t.String()
	case bool:
		// Python's str(True) is "True"; keep the same token so a boolean-valued
		// tag reads identically on both sides.
		if t {
			s = "True"
		} else {
			s = "False"
		}
	default:
		s = fmt.Sprintf("%v", t)
	}
	return truncateRunes(cleanStr(s), exifStrCap)
}
