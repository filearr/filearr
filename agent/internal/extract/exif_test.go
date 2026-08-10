package extract

import (
	"context"
	"encoding/json"
	"math"
	"path/filepath"
	"strings"
	"testing"
	"unicode/utf8"
)

// The mapping is tested as a PURE function over a decoded tag object, which is
// what makes parity with backend/filearr/exif.py map_exif_tags checkable at all:
// no exiftool is ever executed here, on any platform.

func mapped(raw map[string]any) map[string]any {
	res := &Result{Meta: map[string]any{}}
	mapExifTags(raw, res)
	return res.Meta
}

func wantKey(t *testing.T, m map[string]any, key string, want any) {
	t.Helper()
	got, ok := m[key]
	if !ok {
		t.Fatalf("%s: missing (have %v)", key, m)
	}
	if got != want {
		t.Fatalf("%s = %#v (%T), want %#v (%T)", key, got, got, want, want)
	}
}

func wantAbsent(t *testing.T, m map[string]any, key string) {
	t.Helper()
	if v, ok := m[key]; ok {
		t.Fatalf("%s should be absent, got %#v", key, v)
	}
}

func TestMapExifTagsFullTagSet(t *testing.T) {
	m := mapped(map[string]any{
		"Make":             "Canon",
		"Model":            "EOS R5",
		"LensModel":        "RF24-70mm F2.8 L IS USM",
		"LensID":           "RF24-70mm",
		"ISO":              json.Number("100"),
		"ExposureTime":     json.Number("0.004"),
		"FNumber":          json.Number("2.8"),
		"FocalLength":      json.Number("35"),
		"ImageWidth":       json.Number("8192"),
		"ImageHeight":      json.Number("5464"),
		"DateTimeOriginal": "2026:07:04 11:22:33",
		"CreateDate":       "2026:07:04 11:22:34",
		"ModifyDate":       "2026:07:05 09:00:00",
		"GPSLatitude":      json.Number("47.6062"),
		"GPSLongitude":     json.Number("-122.3321"),
		"GPSAltitude":      json.Number("56.25"),
		"Ignored":          "not in the curated map",
	})

	wantKey(t, m, "exif.camera_make", "Canon")
	wantKey(t, m, "exif.camera_model", "EOS R5")
	wantKey(t, m, "exif.lens_model", "RF24-70mm F2.8 L IS USM")
	wantKey(t, m, "exif.lens_id", "RF24-70mm")
	// Integer-valued tags stay integral (UseNumber): central stores 100, not 100.0.
	wantKey(t, m, "exif.iso", int64(100))
	wantKey(t, m, "exif.exposure_time", 0.004)
	wantKey(t, m, "exif.f_number", 2.8)
	wantKey(t, m, "exif.focal_length", int64(35))
	wantKey(t, m, "exif.width", int64(8192))
	wantKey(t, m, "exif.height", int64(5464))
	// DateTimeOriginal wins; CreateDate is only a fallback.
	wantKey(t, m, "exif.taken_at", "2026:07:04 11:22:33")
	wantAbsent(t, m, "Ignored")
}

// GPS is stored RAW by the extractor. The exposure decision belongs to central's
// strip_gps gate plus the per-library expose_gps flag (CWE-1230) — if this test
// ever starts failing because someone filtered GPS here, the fix is to revert
// that, not to update the test.
func TestMapExifTagsGPSStoredRaw(t *testing.T) {
	m := mapped(map[string]any{
		"GPSLatitude":  json.Number("47.6062"),
		"GPSLongitude": json.Number("-122.3321"),
		"GPSAltitude":  json.Number("56.25"),
	})
	wantKey(t, m, "exif.gps_latitude", 47.6062)
	wantKey(t, m, "exif.gps_longitude", -122.3321)
	wantKey(t, m, "exif.gps_altitude", 56.25)
}

func TestMapExifTagsCreateDateFallback(t *testing.T) {
	t.Run("used when DateTimeOriginal absent", func(t *testing.T) {
		m := mapped(map[string]any{"CreateDate": "2026:01:02 03:04:05"})
		wantKey(t, m, "exif.taken_at", "2026:01:02 03:04:05")
	})
	t.Run("used when DateTimeOriginal is empty", func(t *testing.T) {
		// central's _str returns None for a whitespace-only value, so setdefault
		// never fires for the first row and CreateDate fills the key.
		m := mapped(map[string]any{
			"DateTimeOriginal": "   ",
			"CreateDate":       "2026:01:02 03:04:05",
		})
		wantKey(t, m, "exif.taken_at", "2026:01:02 03:04:05")
	})
	t.Run("never displaces a real DateTimeOriginal", func(t *testing.T) {
		m := mapped(map[string]any{
			"DateTimeOriginal": "2020:05:05 05:05:05",
			"CreateDate":       "2026:01:02 03:04:05",
		})
		wantKey(t, m, "exif.taken_at", "2020:05:05 05:05:05")
	})
}

func TestMapExifTagsNumericCoercion(t *testing.T) {
	t.Run("numeric strings become floats", func(t *testing.T) {
		// Python's float("400") is 400.0, so central stores a float here even
		// though the same tag as a JSON int would stay integral.
		m := mapped(map[string]any{"ISO": "400", "FNumber": " 1.8 "})
		wantKey(t, m, "exif.iso", 400.0)
		wantKey(t, m, "exif.f_number", 1.8)
	})
	t.Run("booleans are rejected", func(t *testing.T) {
		m := mapped(map[string]any{"ISO": true, "FNumber": false})
		wantAbsent(t, m, "exif.iso")
		wantAbsent(t, m, "exif.f_number")
	})
	t.Run("NaN and infinities are dropped", func(t *testing.T) {
		m := mapped(map[string]any{
			"ISO":          math.NaN(),
			"FNumber":      "nan",
			"FocalLength":  math.Inf(1),
			"ExposureTime": "inf",
		})
		wantAbsent(t, m, "exif.iso")
		wantAbsent(t, m, "exif.f_number")
		wantAbsent(t, m, "exif.focal_length")
		wantAbsent(t, m, "exif.exposure_time")
	})
	t.Run("non-numeric values are dropped", func(t *testing.T) {
		m := mapped(map[string]any{"ISO": "auto", "FNumber": nil})
		wantAbsent(t, m, "exif.iso")
		wantAbsent(t, m, "exif.f_number")
	})
	t.Run("plain Go numbers are accepted", func(t *testing.T) {
		m := mapped(map[string]any{"ISO": 200, "FNumber": 4.0})
		wantKey(t, m, "exif.iso", int64(200))
		wantKey(t, m, "exif.f_number", 4.0)
	})
}

func TestMapExifTagsStringHandling(t *testing.T) {
	t.Run("capped at 500 runes not bytes", func(t *testing.T) {
		// Multi-byte runes: a byte cap would cut this to 250 characters.
		m := mapped(map[string]any{"Make": strings.Repeat("é", 600)})
		got, _ := m["exif.camera_make"].(string)
		if n := utf8.RuneCountInString(got); n != exifStrCap {
			t.Fatalf("camera_make = %d runes, want %d", n, exifStrCap)
		}
	})
	t.Run("trimmed and control-stripped", func(t *testing.T) {
		m := mapped(map[string]any{"Model": "  EOS\x00 R5\x1b[31m  "})
		wantKey(t, m, "exif.camera_model", "EOS R5[31m")
	})
	t.Run("empty after normalisation is omitted", func(t *testing.T) {
		m := mapped(map[string]any{"Make": "\x00\x01", "LensModel": "   "})
		wantAbsent(t, m, "exif.camera_make")
		wantAbsent(t, m, "exif.lens_model")
	})
}

func TestMapExifTagsFlatKeys(t *testing.T) {
	t.Run("make and model", func(t *testing.T) {
		m := mapped(map[string]any{"Make": "Canon", "Model": "EOS R5"})
		wantKey(t, m, "camera", "Canon EOS R5")
	})
	t.Run("make only", func(t *testing.T) {
		m := mapped(map[string]any{"Make": "Canon"})
		wantKey(t, m, "camera", "Canon")
	})
	t.Run("model only", func(t *testing.T) {
		m := mapped(map[string]any{"Model": "EOS R5"})
		wantKey(t, m, "camera", "EOS R5")
	})
	t.Run("neither", func(t *testing.T) {
		m := mapped(map[string]any{"ISO": json.Number("100")})
		wantAbsent(t, m, "camera")
	})
	t.Run("flat taken_at comes from ModifyDate, not DateTimeOriginal", func(t *testing.T) {
		// central's flat key is EXIF tag 306 (DateTime), whose exiftool name is
		// ModifyDate. Changing this to DateTimeOriginal would silently disagree
		// with every centrally-scanned edited photo.
		m := mapped(map[string]any{
			"DateTimeOriginal": "2020:05:05 05:05:05",
			"ModifyDate":       "2026:07:05 09:00:00",
		})
		wantKey(t, m, "taken_at", "2026:07:05 09:00:00")
		wantKey(t, m, "exif.taken_at", "2020:05:05 05:05:05")
	})
	t.Run("no ModifyDate leaves the flat key absent", func(t *testing.T) {
		m := mapped(map[string]any{"DateTimeOriginal": "2020:05:05 05:05:05"})
		wantAbsent(t, m, "taken_at")
	})
	t.Run("an existing value is never overwritten", func(t *testing.T) {
		res := &Result{Meta: map[string]any{"camera": "already here", "taken_at": "already here"}}
		mapExifTags(map[string]any{"Make": "Canon", "ModifyDate": "2026:07:05 09:00:00"}, res)
		wantKey(t, res.Meta, "camera", "already here")
		wantKey(t, res.Meta, "taken_at", "already here")
	})
}

// A tool-level failure must degrade to the _exif_error sentinel and return nil:
// a returned error would become _extract_error, which central reads as a FAILED
// extraction rather than a supplementary pass that did not run.
func TestExtractEXIFToolFailureIsNotAnError(t *testing.T) {
	res := &Result{Meta: map[string]any{"width": 100}}
	opts := Options{ExiftoolPath: filepath.Join(t.TempDir(), "no-such-exiftool-binary")}

	if err := extractEXIF(context.Background(), filepath.Join(t.TempDir(), "photo.jpg"), opts, res); err != nil {
		t.Fatalf("extractEXIF returned an error for a missing binary: %v", err)
	}
	msg, ok := res.Meta["_exif_error"].(string)
	if !ok || msg == "" {
		t.Fatalf("_exif_error not recorded: %#v", res.Meta)
	}
	if utf8.RuneCountInString(msg) > exifStrCap {
		t.Fatalf("_exif_error is %d runes, over the %d cap", utf8.RuneCountInString(msg), exifStrCap)
	}
	if _, ok := res.Meta["exif.camera_make"]; ok {
		t.Fatalf("a failed run must not emit exif.* keys: %#v", res.Meta)
	}
	wantKey(t, res.Meta, "width", 100) // the image pass's result survives
}

func TestDecodeExiftoolJSON(t *testing.T) {
	t.Run("array of one object", func(t *testing.T) {
		m, err := decodeExiftoolJSON([]byte(`[{"Make":"Canon"}]`))
		if err != nil {
			t.Fatal(err)
		}
		if m["Make"] != "Canon" {
			t.Fatalf("got %#v", m)
		}
	})
	t.Run("bare object", func(t *testing.T) {
		m, err := decodeExiftoolJSON([]byte(`{"Make":"Canon"}`))
		if err != nil || m["Make"] != "Canon" {
			t.Fatalf("got %#v, %v", m, err)
		}
	})
	t.Run("empty array", func(t *testing.T) {
		m, err := decodeExiftoolJSON([]byte(`[]`))
		if err != nil || len(m) != 0 {
			t.Fatalf("got %#v, %v", m, err)
		}
	})
	t.Run("garbage", func(t *testing.T) {
		if _, err := decodeExiftoolJSON([]byte("not json")); err == nil {
			t.Fatal("expected an error")
		}
	})
	t.Run("array of a non-object", func(t *testing.T) {
		if _, err := decodeExiftoolJSON([]byte(`[42]`)); err == nil {
			t.Fatal("expected an error")
		}
	})
}
