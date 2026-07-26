package thumbs

import (
	"encoding/binary"
	"math"
	"os"
	"path/filepath"
	"testing"
)

// writeBinarySTL emits a minimal valid binary STL: an axis-aligned unit
// tetrahedron (4 faces). Binary layout: 80-byte header, uint32 count, then per
// triangle: normal (3xf32) + 3 vertices (9xf32) + uint16 attr.
func writeBinarySTL(t *testing.T, path string) {
	t.Helper()
	verts := [][3][3]float32{
		{{0, 0, 0}, {1, 0, 0}, {0, 1, 0}},
		{{0, 0, 0}, {1, 0, 0}, {0, 0, 1}},
		{{0, 0, 0}, {0, 1, 0}, {0, 0, 1}},
		{{1, 0, 0}, {0, 1, 0}, {0, 0, 1}},
	}
	f, err := os.Create(path)
	if err != nil {
		t.Fatalf("create stl: %v", err)
	}
	defer f.Close()
	if _, err := f.Write(make([]byte, 80)); err != nil {
		t.Fatalf("header: %v", err)
	}
	if err := binary.Write(f, binary.LittleEndian, uint32(len(verts))); err != nil {
		t.Fatalf("count: %v", err)
	}
	for _, tri := range verts {
		var normal [3]float32 // fauxgl recomputes; zero normal is valid STL
		if err := binary.Write(f, binary.LittleEndian, normal); err != nil {
			t.Fatalf("normal: %v", err)
		}
		for _, v := range tri {
			if err := binary.Write(f, binary.LittleEndian, v); err != nil {
				t.Fatalf("vertex: %v", err)
			}
		}
		if err := binary.Write(f, binary.LittleEndian, uint16(0)); err != nil {
			t.Fatalf("attr: %v", err)
		}
	}
}

func TestGenerateModelThumbRendersSTL(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "part.stl")
	writeBinarySTL(t, p)
	tb := GenerateModelThumb(p, GridSpec)
	if tb == nil {
		t.Fatal("expected a thumbnail for a valid STL")
	}
	if tb.Width != GridSpec.MaxEdge || tb.Height != GridSpec.MaxEdge {
		t.Fatalf("got %dx%d, want %dx%d square", tb.Width, tb.Height, GridSpec.MaxEdge, GridSpec.MaxEdge)
	}
	if len(tb.Data) == 0 || len(tb.Data) > GridSpec.MaxBytes {
		t.Fatalf("encoded size %d outside (0, %d]", len(tb.Data), GridSpec.MaxBytes)
	}
	// The render must not be a blank background: expect some pixel variance.
	if math.Abs(float64(len(tb.Data))) < 500 {
		t.Fatalf("suspiciously tiny encode (%d bytes) — blank render?", len(tb.Data))
	}
}

func TestGenerateModelThumbHostileInputs(t *testing.T) {
	dir := t.TempDir()

	junk := filepath.Join(dir, "junk.stl")
	if err := os.WriteFile(junk, []byte("this is not a mesh"), 0o600); err != nil {
		t.Fatal(err)
	}
	if tb := GenerateModelThumb(junk, GridSpec); tb != nil {
		t.Fatal("junk STL must yield nil, not a thumbnail")
	}

	empty := filepath.Join(dir, "empty.stl")
	if err := os.WriteFile(empty, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	if tb := GenerateModelThumb(empty, GridSpec); tb != nil {
		t.Fatal("empty STL must yield nil")
	}

	// Non-STL model extensions are skipped (central's trimesh path owns them).
	obj := filepath.Join(dir, "part.obj")
	if err := os.WriteFile(obj, []byte("v 0 0 0"), 0o600); err != nil {
		t.Fatal(err)
	}
	if tb := GenerateModelThumb(obj, GridSpec); tb != nil {
		t.Fatal("non-STL extension must be skipped on the agent")
	}

	if tb := GenerateModelThumb(filepath.Join(dir, "missing.stl"), GridSpec); tb != nil {
		t.Fatal("missing file must yield nil")
	}
}
