package thumbs

// 3D-model previews (roadmap §20). fauxgl is a PURE-GO software renderer with a
// built-in STL loader (MIT) — the only kind of renderer a CGO_ENABLED=0 static
// binary can carry. STL only on the agent (3MF needs the mesh unzipped from its
// container first — central's trimesh path handles those); other model formats
// return nil and fall through to the placeholder icon.

import (
	"os"
	"path/filepath"
	"strings"

	"github.com/fogleman/fauxgl"
)

const (
	// modelMaxBytes caps the source file the renderer will load — fauxgl reads
	// the whole mesh into RAM (mirrors central's model3d_max_bytes posture, but
	// conservative: agents run on shared hosts, not a dedicated worker).
	modelMaxBytes = 256 << 20 // 256 MiB
	// modelMaxTriangles caps the render workload — fauxgl iterates triangles in
	// Go, so a pathological mesh must be bounded (hostile-file discipline).
	modelMaxTriangles = 3_000_000
	// Supersample factor: render large, downscale in encodeCapped — fauxgl has
	// no MSAA; 2x + the JPEG ladder's downscale antialiases edges.
	modelSupersample = 2
)

// GenerateModelThumb renders a shaded isometric preview of an STL file. Returns
// nil for ANY failure (not STL, oversized, unparsable, empty mesh) — a bad file
// never produces an error, only "no thumbnail", exactly like every other
// generator here.
func GenerateModelThumb(path string, spec TierSpec) *ThumbBytes {
	if !strings.EqualFold(filepath.Ext(path), ".stl") || spec.MaxEdge <= 0 {
		return nil
	}
	if fi, err := os.Stat(path); err != nil || fi.Size() > modelMaxBytes {
		return nil
	}
	defer func() { _ = recover() }() // fauxgl on hostile geometry: never panic the pass

	mesh, err := fauxgl.LoadSTL(path)
	if err != nil || mesh == nil || len(mesh.Triangles) == 0 ||
		len(mesh.Triangles) > modelMaxTriangles {
		return nil
	}
	// Fit the model into the bi-unit cube around the origin so one fixed camera
	// frames every model regardless of its native units/placement.
	mesh.BiUnitCube()

	size := spec.MaxEdge * modelSupersample
	ctx3d := fauxgl.NewContext(size, size)
	// Slicer-style neutral scene (matches central's palette): slate-100
	// background, steel-blue material.
	ctx3d.ClearColorBufferWith(fauxgl.HexColor("#f1f5f9"))

	eye := fauxgl.V(1, -1, 0.75).Normalize().MulScalar(3)
	center := fauxgl.V(0, 0, 0)
	up := fauxgl.V(0, 0, 1)
	light := fauxgl.V(0.5, -0.6, 0.7).Normalize()

	matrix := fauxgl.LookAt(eye, center, up).Perspective(30, 1, 1, 10)
	shader := fauxgl.NewPhongShader(matrix, light, eye)
	shader.ObjectColor = fauxgl.HexColor("#5a7c9a")
	ctx3d.Shader = shader
	ctx3d.DrawMesh(mesh)

	return encodeCapped(ctx3d.Image(), spec)
}
