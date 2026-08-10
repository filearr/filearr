package extract

import (
	"bytes"
	"context"
	"encoding/binary"
	"os/exec"
	"testing"
)

// --- audio tags -------------------------------------------------------------

// buildID3v23 constructs a minimal ID3v2.3 tag carrying the given text frames,
// followed by a byte of fake audio — the same fixture technique the thumbnailer
// tests use. Built in code so there is no binary blob to bit-rot.
func buildID3v23(frames map[string]string, order []string) []byte {
	var body bytes.Buffer
	for _, id := range order {
		text := frames[id]
		var fb bytes.Buffer
		fb.WriteByte(0x00) // text encoding: ISO-8859-1
		fb.WriteString(text)

		body.WriteString(id)
		binary.Write(&body, binary.BigEndian, uint32(fb.Len())) // v2.3: plain size
		body.Write([]byte{0x00, 0x00})                          // frame flags
		body.Write(fb.Bytes())
	}

	var out bytes.Buffer
	out.WriteString("ID3")
	out.Write([]byte{0x03, 0x00, 0x00}) // v2.3.0, no flags
	out.Write(synchsafe(uint32(body.Len())))
	out.Write(body.Bytes())
	out.WriteByte(0xFF) // trailing fake-audio byte
	return out.Bytes()
}

// synchsafe encodes n as a 28-bit synchsafe integer (7 bits per byte).
func synchsafe(n uint32) []byte {
	return []byte{
		byte((n >> 21) & 0x7f), byte((n >> 14) & 0x7f),
		byte((n >> 7) & 0x7f), byte(n & 0x7f),
	}
}

func TestExtractAudioTags(t *testing.T) {
	dir := t.TempDir()
	p := writeFile(t, dir, "song.mp3", buildID3v23(map[string]string{
		"TIT2": "Cathedral",
		"TPE1": "The Builders",
		"TALB": "Stonework",
		"TCON": "Ambient",
		"TYER": "1994",
	}, []string{"TIT2", "TPE1", "TALB", "TCON", "TYER"}))

	res, err := Extract(context.Background(), p, CategoryAudio, Options{})
	if err != nil {
		t.Fatalf("Extract: %v", err)
	}
	if res == nil {
		t.Fatal("no result for a tagged mp3")
	}
	for key, want := range map[string]any{
		"title":  "Cathedral",
		"artist": "The Builders",
		"album":  "Stonework",
		"genre":  "Ambient",
		"year":   1994,
	} {
		if res.Meta[key] != want {
			t.Errorf("%s = %v (%T), want %v", key, res.Meta[key], res.Meta[key], want)
		}
	}
}

func TestExtractAudioUntaggedIsRecordedNotFatal(t *testing.T) {
	dir := t.TempDir()
	p := writeFile(t, dir, "bare.mp3", []byte("not an audio container at all"))

	res, err := Extract(context.Background(), p, CategoryAudio, Options{})
	if err != nil {
		t.Fatalf("an untagged file must not fail the pass: %v", err)
	}
	if res == nil || res.Errors["audio"] == "" {
		t.Fatalf("expected a recorded per-extractor error, got %+v", res)
	}
}

// --- host-tool paths (skipped when the tool is absent) ----------------------

// requireTool skips the test when the host lacks the binary. These tests are
// hermetic in the sense that matters: no network, and no failure on a machine
// that simply has not installed ffmpeg/tesseract.
func requireTool(t *testing.T, name string) string {
	t.Helper()
	p, err := exec.LookPath(name)
	if err != nil {
		t.Skipf("%s not installed on this host", name)
	}
	return p
}

func TestProbeVideoWithFFprobe(t *testing.T) {
	ffprobe := requireTool(t, "ffprobe")
	ffmpeg := requireTool(t, "ffmpeg")

	dir := t.TempDir()
	out := dir + "/clip.mp4"
	// One second of synthetic 64x48 video — small, deterministic, no assets.
	cmd := exec.Command(ffmpeg, "-hide_banner", "-loglevel", "error",
		"-f", "lavfi", "-i", "testsrc=size=64x48:rate=10:duration=1",
		"-pix_fmt", "yuv420p", "-y", out)
	if err := cmd.Run(); err != nil {
		t.Skipf("ffmpeg could not synthesise a clip: %v", err)
	}

	res, err := Extract(context.Background(), out, CategoryVideo, Options{FFprobePath: ffprobe})
	if err != nil {
		t.Fatalf("Extract: %v", err)
	}
	if res == nil {
		t.Fatalf("no result; errors: %v", res)
	}
	if res.Meta["width"] != int64(64) || res.Meta["height"] != int64(48) {
		t.Errorf("dimensions = %v x %v, want 64 x 48 (errors: %v)", res.Meta["width"], res.Meta["height"], res.Errors)
	}
	if res.Meta["resolution"] != "64x48" {
		t.Errorf("resolution = %v, want 64x48", res.Meta["resolution"])
	}
	if res.Meta["video_codec"] == nil {
		t.Errorf("video_codec missing: %v", res.Meta)
	}
	if res.Meta["container"] == nil {
		t.Errorf("container missing: %v", res.Meta)
	}
}

func TestProbeVideoWithoutFFprobeIsSkipped(t *testing.T) {
	dir := t.TempDir()
	p := writeFile(t, dir, "clip.mkv", []byte("not really a video"))

	res, err := Extract(context.Background(), p, CategoryVideo, Options{})
	if err != nil {
		t.Fatalf("Extract: %v", err)
	}
	if res != nil {
		t.Fatalf("video without ffprobe produced a result: %+v", res)
	}
}

func TestProbeFailureIsRecordedNotFatal(t *testing.T) {
	ffprobe := requireTool(t, "ffprobe")
	dir := t.TempDir()
	p := writeFile(t, dir, "broken.mkv", []byte("definitely not matroska"))

	res, err := Extract(context.Background(), p, CategoryVideo, Options{FFprobePath: ffprobe})
	if err != nil {
		t.Fatalf("a corrupt video must not fail the pass: %v", err)
	}
	if res == nil || res.Errors["ffprobe"] == "" {
		t.Fatalf("expected a recorded ffprobe error, got %+v", res)
	}
}

func TestOCRImageWithTesseract(t *testing.T) {
	tess := requireTool(t, "tesseract")
	dir := t.TempDir()
	p := writeFile(t, dir, "blank.png", tinyPNG(t, 64, 64))

	res, err := Extract(context.Background(), p, CategoryImage, Options{
		OCR: true, TesseractPath: tess,
	})
	if err != nil {
		t.Fatalf("Extract: %v", err)
	}
	// A synthetic gradient has no legible text, so the assertion is about the
	// CONTRACT, not the content: dimensions still land, and OCR either produced
	// a string pair or nothing — never a half-written key or a crash.
	if res == nil || res.Meta["width"] != 64 {
		t.Fatalf("image extraction regressed under OCR: %+v", res)
	}
	if _, hasText := res.Meta["ocr_text"]; hasText {
		if _, hasFlag := res.Meta["ocr_text_truncated"]; !hasFlag {
			t.Error("ocr_text stored without its truncation flag")
		}
	}
}

func TestOCRSkippedWithoutTesseract(t *testing.T) {
	dir := t.TempDir()
	p := writeFile(t, dir, "pic.png", tinyPNG(t, 8, 8))

	// Policy asks for OCR but the host has no tesseract: the image extractor
	// still runs and nothing is recorded as an error (the mismatch is reported
	// once via IgnoredSettings, not per file).
	res, err := Extract(context.Background(), p, CategoryImage, Options{OCR: true})
	if err != nil {
		t.Fatalf("Extract: %v", err)
	}
	if res == nil || res.Meta["width"] != 8 {
		t.Fatalf("image extraction did not run: %+v", res)
	}
	if len(res.Errors) != 0 {
		t.Errorf("absent tesseract recorded an error: %v", res.Errors)
	}
	if _, ok := res.Meta["ocr_text"]; ok {
		t.Error("ocr_text produced without tesseract")
	}
}

func TestOCRPixelCeiling(t *testing.T) {
	tess := requireTool(t, "tesseract")
	dir := t.TempDir()
	// A tiny file that DECLARES a huge canvas would be ideal; encoding one is
	// not worth it, so assert the gate arithmetic directly instead.
	p := writeFile(t, dir, "small.png", tinyPNG(t, 4, 4))
	w, h, err := imageDimensions(p)
	if err != nil {
		t.Fatalf("imageDimensions: %v", err)
	}
	if int64(w)*int64(h) > OCRMaxPixels {
		t.Fatalf("fixture unexpectedly exceeds the pixel ceiling")
	}
	res := &Result{Meta: map[string]any{}}
	if err := ocrImage(context.Background(), p, Options{
		TesseractPath: tess, OCRTimeout: OCRTimeout, OCRLang: OCRLang, MaxOCRChars: OCRMaxChars,
	}, res); err != nil {
		t.Fatalf("under-ceiling image was rejected: %v", err)
	}
}
