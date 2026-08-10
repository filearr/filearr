package extract

import (
	"bytes"
	"context"
	"fmt"
	"os/exec"
)

// ocrImage runs ONE tesseract subprocess over a raster image and stores
// ocr_text/ocr_text_truncated — central's OCR key vocabulary (tasks/ocr_run.py),
// so the existing central pipelines (RAG chunk selection keys on ocr_text,
// search projects it into body_text) pick agent-OCR'd items up unchanged.
//
// Scope for this phase: IMAGES ONLY. Central also OCRs scanned PDFs by
// rasterising them with pdftoppm first; that needs a second host tool and the
// PDF work is explicitly a later phase, so a PDF is never handed to tesseract
// here.
//
// The invocation mirrors backend/filearr/ocr.py run_tesseract exactly:
// `tesseract <img> stdout -l <lang> --psm 3`, argv list (never a shell string,
// so nothing in the untrusted path is interpreted), a hard timeout that kills
// the child, and a bounded stdout read.
func ocrImage(ctx context.Context, path string, opts Options, res *Result) error {
	// Pixel ceiling BEFORE the subprocess (mirrors ocr_max_pixels): a declared
	// 100 MP scan is refused at the header, never rasterised by tesseract.
	if w, h, err := imageDimensions(path); err == nil {
		if int64(w)*int64(h) > OCRMaxPixels {
			return fmt.Errorf("ocr guard: %dx%d exceeds %d pixel ceiling", w, h, OCRMaxPixels)
		}
	}

	cctx, cancel := context.WithTimeout(ctx, opts.OCRTimeout)
	defer cancel()

	argv := []string{path, "stdout", "-l", opts.OCRLang, "--psm", OCRPSM}
	cmd := exec.CommandContext(cctx, opts.TesseractPath, argv...)
	// Bound stdout at the stored-character cap times UTF-8's worst case: past
	// that point the text would be truncated anyway, so there is no reason to
	// buffer it.
	out := &capBuffer{limit: opts.MaxOCRChars*4 + 8}
	var stderr bytes.Buffer
	cmd.Stdout = out
	cmd.Stderr = &stderr

	if err := cmd.Run(); err != nil {
		if cctx.Err() == context.DeadlineExceeded {
			return fmt.Errorf("tesseract timed out after %s", opts.OCRTimeout)
		}
		if msg := lastLine(stderr.String()); msg != "" {
			return fmt.Errorf("tesseract failed: %s", msg)
		}
		return fmt.Errorf("tesseract failed: %w", err)
	}

	text, truncated := normalizeBodyText(out.buf.String(), opts.MaxOCRChars, out.overflowed)
	if text == "" {
		return nil // a blank page is a legitimate result, not a failure
	}
	res.Meta["ocr_text"] = text
	res.Meta["ocr_text_truncated"] = truncated
	return nil
}
