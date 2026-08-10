package extract

import (
	"bufio"
	"fmt"
	"image"
	"image/color"
	"os"
	"strings"

	// stdlib raster decoders (side-effect registration for image.DecodeConfig).
	_ "image/gif"
	_ "image/jpeg"
	_ "image/png"

	// pure-Go extra decoders, already vendored for the thumbnailer.
	_ "golang.org/x/image/bmp"
	_ "golang.org/x/image/tiff"
	_ "golang.org/x/image/webp"
)

// extractImage fills width/height/format/mode from the image HEADER only.
//
// image.DecodeConfig reads just enough of the stream to answer those questions —
// it never allocates or decodes the pixel plane, so a 50000x50000 declared PNG
// costs a few hundred bytes here instead of 10 GB. That is also why there is no
// decompression-bomb guard in this function: there is nothing to bomb.
//
// camera/taken_at (the EXIF keys in central's vocabulary) are deliberately NOT
// produced yet — deep EXIF is the exiftool phase of the parity plan.
func extractImage(path string, res *Result) error {
	f, err := os.Open(path)
	if err != nil {
		return fmt.Errorf("open image: %w", err)
	}
	defer f.Close()

	cfg, format, err := image.DecodeConfig(bufio.NewReader(f))
	if err != nil {
		return fmt.Errorf("decode image header: %w", err)
	}
	res.Meta["width"] = cfg.Width
	res.Meta["height"] = cfg.Height
	// Go registers decoders under lower-case names ("png", "jpeg", …); central
	// stores Pillow's Image.format, which is the same token upper-cased ("PNG",
	// "JPEG", "GIF", "BMP", "TIFF", "WEBP"). Upper-casing gives exact parity for
	// every format both sides can read.
	res.set("format", strings.ToUpper(format))
	if mode := pillowMode(cfg.ColorModel); mode != "" {
		res.Meta["mode"] = mode
	}
	return nil
}

// pillowMode maps a Go color model onto Pillow's `Image.mode` token, so the
// `mode` key means the same thing whichever side extracted the item. Unknown
// models yield "" and the key is simply omitted (a missing key is honest; a
// wrong one is not).
func pillowMode(m color.Model) string {
	switch m {
	case color.RGBAModel, color.NRGBAModel, color.RGBA64Model, color.NRGBA64Model:
		return "RGBA"
	case color.GrayModel, color.Gray16Model:
		return "L"
	case color.AlphaModel, color.Alpha16Model:
		return "L"
	case color.CMYKModel:
		return "CMYK"
	case color.YCbCrModel:
		return "RGB" // JPEG: Pillow reports RGB for a YCbCr-coded baseline image
	case color.NYCbCrAModel:
		return "RGBA"
	}
	// GIF and other paletted images carry a color.Palette (a slice type), which
	// is not comparable to the singletons above.
	if _, ok := m.(color.Palette); ok {
		return "P"
	}
	return ""
}

// imageDimensions re-reads just the header for the OCR pixel gate. Cheap enough
// to repeat (a header read), and keeping it separate means the OCR path does not
// depend on the image extractor having succeeded.
func imageDimensions(path string) (w, h int, err error) {
	f, err := os.Open(path)
	if err != nil {
		return 0, 0, err
	}
	defer f.Close()
	cfg, _, err := image.DecodeConfig(bufio.NewReader(f))
	if err != nil {
		return 0, 0, err
	}
	return cfg.Width, cfg.Height, nil
}
