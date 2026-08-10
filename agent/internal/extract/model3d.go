package extract

import (
	"archive/zip"
	"bufio"
	"context"
	"encoding/binary"
	"encoding/json"
	"encoding/xml"
	"fmt"
	"io"
	"math"
	"os"
	"strconv"
	"strings"
)

// Ceilings for the 3D pass. model3dMaxBytes mirrors central; the rest are the
// agent's own work bounds, which central does not need because trimesh's own
// loaders (and the 512 MiB file gate in front of them) box the work for it.
// model3dMaxBytes mirrors backend/filearr/config.py Settings.model3d_max_bytes
// (= 536_870_912, 512 MiB). The policy's extract_max_bytes gate has already run
// in Extract() and is usually MUCH smaller, so in practice this ceiling only
// fires for a library configured with a very large (or absent) policy limit — it
// is the parser's own last line of defence, not a duplicate of the policy check.
//
// It is a var rather than a const for exactly one reason: the guard test lowers
// it instead of writing a 512 MiB fixture. Nothing in production assigns to it.
var model3dMaxBytes int64 = 536_870_912

const (
	// model3dMaxVertices / model3dMaxTriangles bound the element counts a single
	// file may declare. A 512 MiB binary STL tops out near 10.7M triangles, so
	// these are generous for honest files and still stop a text format from
	// claiming an unbounded count.
	model3dMaxVertices  = 20_000_000
	model3dMaxTriangles = 20_000_000

	// model3dMaxFaceVerts bounds ONE polygon's vertex list. A declared face count
	// is a length prefix in the binary formats, i.e. an allocation an attacker
	// picks; no honest polygon has a million corners.
	model3dMaxFaceVerts = 1_000_000

	// model3dWatertightMaxTriangles is the work ceiling past which the watertight
	// computation is abandoned and the key OMITTED. It is deliberately far below
	// the triangle ceiling: the check needs a merged-vertex index plus one
	// [3]uint32 per triangle plus an edge map, so its memory is a multiple of the
	// mesh size — at 1M triangles that is already a few hundred MB, and this pass
	// runs INLINE in the scan walk.
	model3dWatertightMaxTriangles = 1_000_000

	// model3dMaxLineBytes bounds one line of the text formats. bufio.Scanner's
	// default 64 KiB token limit is NOT enough for real OBJ files — exporters emit
	// multi-kilobyte generator banners and some tools write a whole face list on
	// one line — and a scanner that errors there would turn ordinary models into a
	// per-scan error, so the buffer is raised deliberately rather than left at the
	// default.
	model3dMaxLineBytes = 4 << 20

	// model3dHeaderMaxBytes bounds the PLY/OFF ASCII header, which is read before
	// any count is known and is otherwise an unbounded read.
	model3dHeaderMaxBytes = 1 << 20

	// model3dJSONMax bounds the glTF/GLB JSON document (the GLB chunk length is
	// attacker-declared, so it is checked before the allocation, not after).
	model3dJSONMax int64 = 64 << 20

	// model3dCancelEvery is how often the parse loops check ctx. Per-element would
	// dominate the loop for a 10M-triangle mesh; a few thousand elements is well
	// under a millisecond of extra latency on cancellation.
	model3dCancelEvery = 4096
)

// model3dGeometryExts mirrors backend/filearr/tasks/model3d.py _GEOMETRY_EXTS —
// the formats central can load as geometry with trimesh's dependency-free stack.
// Everything else in the three-d-cad category (step/stp/fbx/blend/dwg/…) has no
// safe pure loader on either side and is reported as `unsupported`, which is a
// FACT about the format, not an error.
var model3dGeometryExts = map[string]bool{
	"stl": true, "obj": true, "ply": true, "off": true,
	"gltf": true, "glb": true, "3mf": true,
}

// extractModel3D fills central's geometry vocabulary (model3d.py's docstring
// schema: triangles/vertices/mesh_count/bbox/bbox_volume/watertight/file_format/
// unsupported) using purpose-written pure-Go parsers.
//
// Central uses trimesh; the agent deliberately does not pull in a Go mesh
// library. The only vendored candidate is fauxgl, which is a RENDERER — it loads
// whole meshes into RAM for the thumbnailer, which is exactly the allocation
// profile an untrusted-file pass must not have. Counting triangles and tracking
// a bounding box is a streaming job, so it is done streaming.
func extractModel3D(ctx context.Context, path, ext string, opts Options, res *Result) error {
	if !model3dGeometryExts[ext] {
		res.Meta["unsupported"] = true
		return nil
	}

	info, err := os.Stat(path)
	if err != nil {
		return fmt.Errorf("cannot stat model: %w", err)
	}
	if info.Size() > model3dMaxBytes {
		// Central raises Model3DError(kind="guard") here; the agent has no error
		// taxonomy on the wire, so the word "guard" in the message is what tells an
		// operator this was a ceiling and not a corrupt file.
		return fmt.Errorf("model3d guard: model too large (%d > %d bytes)", info.Size(), model3dMaxBytes)
	}

	// watertight is only computed for the formats whose geometry we actually read.
	// glTF/GLB counts come from accessor metadata, never from the binary buffers.
	mb := newMeshBuilder(ext != "gltf" && ext != "glb")

	switch ext {
	case "stl":
		err = parseSTL(ctx, path, info.Size(), mb)
	case "obj":
		err = parseOBJ(ctx, path, mb)
	case "ply":
		err = parsePLY(ctx, path, mb)
	case "off":
		err = parseOFF(ctx, path, mb)
	case "gltf", "glb":
		err = parseGLTF(ctx, path, ext == "glb", mb)
	case "3mf":
		err = parse3MF(ctx, path, mb)
	}
	if err != nil {
		return err
	}
	if mb.triangles == 0 && mb.vertices == 0 {
		// Mirrors model3d.py's "no mesh geometry found in file" guard: trimesh
		// yields no Trimesh for an empty or geometry-less container either.
		return fmt.Errorf("model3d guard: no mesh geometry found in file")
	}
	mb.emit(ext, res)
	return nil
}

// meshBuilder accumulates the geometry facts every parser produces. It holds no
// mesh: positions feed a running bounding box and are discarded, and the only
// retained structure is the watertight bookkeeping, which is dropped the moment
// its ceiling is passed.
type meshBuilder struct {
	triangles int
	vertices  int
	meshCount int

	min, max [3]float64
	haveBBox bool

	// watertight bookkeeping. wt goes false permanently once the work ceiling is
	// hit (or for the formats we refuse to guess for), and the key is then omitted
	// rather than reported as a default.
	wt    bool
	index map[[3]uint64]uint32 // merged vertex position -> merged id
	ids   []uint32             // merged id per source vertex ordinal
	faces [][3]uint32          // triangles as merged-id triples
}

func newMeshBuilder(watertight bool) *meshBuilder {
	b := &meshBuilder{wt: watertight}
	if watertight {
		b.index = map[[3]uint64]uint32{}
	}
	return b
}

// beginMesh starts a new mesh. mesh_count is 1 for the single-mesh formats and
// the object count for a glTF/3MF scene, matching central's len(meshes) over
// trimesh's Scene.geometry.
func (b *meshBuilder) beginMesh() { b.meshCount++ }

// addVertex records one source vertex: it grows the bounding box, counts the
// vertex, and (while watertight is still live) appends its merged id so faces can
// resolve their indices later.
func (b *meshBuilder) addVertex(x, y, z float64) error {
	if b.vertices >= model3dMaxVertices {
		return fmt.Errorf("model3d guard: vertex count exceeds %d", model3dMaxVertices)
	}
	b.vertices++
	b.growBBox(x, y, z)
	if b.wt {
		b.ids = append(b.ids, b.mergedID(x, y, z))
	}
	return nil
}

// addTriangleXYZ records a triangle given as three raw positions — the STL path,
// which has no vertex table at all.
func (b *meshBuilder) addTriangleXYZ(t [3][3]float64) error {
	if b.triangles >= model3dMaxTriangles {
		return fmt.Errorf("model3d guard: triangle count exceeds %d", model3dMaxTriangles)
	}
	b.triangles++
	// STL is a vertex SOUP: every triangle carries its own three positions and
	// nothing is shared. trimesh with process=False reports exactly that, so
	// vertices == 3*triangles here. It is why an STL cube reports 36 vertices
	// where the same cube as an OBJ reports 8 — both sides agree, because both
	// refuse to merge untrusted geometry.
	b.vertices += 3
	for _, p := range t {
		b.growBBox(p[0], p[1], p[2])
	}
	if !b.wt {
		return nil
	}
	if b.triangles > model3dWatertightMaxTriangles {
		b.dropWatertight()
		return nil
	}
	var f [3]uint32
	for i, p := range t {
		f[i] = b.mergedID(p[0], p[1], p[2])
	}
	b.faces = append(b.faces, f)
	return nil
}

// addFace records one polygon by vertex ordinal (0-based over every addVertex
// call so far) and fan-triangulates it: an n-gon contributes n-2 triangles from
// its first corner, which is what trimesh does for OBJ/PLY/OFF polygons and
// therefore what central counts.
func (b *meshBuilder) addFace(idx []int) error {
	n := len(idx)
	if n < 3 {
		return nil // a point or an edge is not a face; trimesh drops it too
	}
	for _, i := range idx {
		if i < 0 || i >= b.vertices {
			return fmt.Errorf("face references vertex %d outside the %d declared vertices", i, b.vertices)
		}
	}
	if b.triangles > model3dMaxTriangles-(n-2) {
		return fmt.Errorf("model3d guard: triangle count exceeds %d", model3dMaxTriangles)
	}
	b.triangles += n - 2
	if !b.wt {
		return nil
	}
	if b.triangles > model3dWatertightMaxTriangles {
		b.dropWatertight()
		return nil
	}
	for k := 1; k+1 < n; k++ {
		b.faces = append(b.faces, [3]uint32{b.ids[idx[0]], b.ids[idx[k]], b.ids[idx[k+1]]})
	}
	return nil
}

// addCounts records element counts declared by METADATA rather than read from
// geometry — the glTF accessor path, where nothing is decoded.
func (b *meshBuilder) addCounts(vertices, triangles int) error {
	if vertices < 0 || triangles < 0 {
		return fmt.Errorf("glTF accessor declares a negative count")
	}
	if b.vertices > model3dMaxVertices-vertices {
		return fmt.Errorf("model3d guard: vertex count exceeds %d", model3dMaxVertices)
	}
	if b.triangles > model3dMaxTriangles-triangles {
		return fmt.Errorf("model3d guard: triangle count exceeds %d", model3dMaxTriangles)
	}
	b.vertices += vertices
	b.triangles += triangles
	return nil
}

// mergedID maps a position onto a merged vertex id by EXACT float-bit equality —
// no epsilon. An epsilon merge is a repair operation, and repairing untrusted
// geometry is both expensive and a judgement call the extractor has no business
// making. Negative zero is folded onto positive zero because the two are equal
// as numbers and differ only in their bit pattern, which would otherwise split a
// perfectly closed mesh at the origin.
func (b *meshBuilder) mergedID(x, y, z float64) uint32 {
	if x == 0 {
		x = 0
	}
	if y == 0 {
		y = 0
	}
	if z == 0 {
		z = 0
	}
	key := [3]uint64{math.Float64bits(x), math.Float64bits(y), math.Float64bits(z)}
	if id, ok := b.index[key]; ok {
		return id
	}
	id := uint32(len(b.index))
	b.index[key] = id
	return id
}

// dropWatertight abandons the computation and releases its memory. Once dropped
// it is never resumed: a partial edge count is worse than no answer.
func (b *meshBuilder) dropWatertight() {
	b.wt = false
	b.index = nil
	b.ids = nil
	b.faces = nil
}

func (b *meshBuilder) growBBox(x, y, z float64) {
	p := [3]float64{x, y, z}
	if !b.haveBBox {
		b.min, b.max, b.haveBBox = p, p, true
		return
	}
	for i := 0; i < 3; i++ {
		if p[i] < b.min[i] {
			b.min[i] = p[i]
		}
		if p[i] > b.max[i] {
			b.max[i] = p[i]
		}
	}
}

// watertight answers "is every undirected edge shared by exactly two faces" over
// positions merged by exact bit equality, returning ok=false when the question
// was not computed.
//
// DELIBERATE DIVERGENCE FROM CENTRAL, worth knowing before you compare two
// items: central reads trimesh's is_watertight with process=False, which does NOT
// merge duplicate vertices, so an STL soup (where every triangle owns its own
// copies of the corner positions) reports FALSE there even for a geometrically
// closed cube. The agent merges by exact bits first and therefore answers TRUE
// for that same cube. An operator diffing an agent-scanned model against a
// centrally-scanned one will see this on STL in particular; the agent's answer is
// the geometrically honest one, and neither side is doing mesh repair.
func (b *meshBuilder) watertight() (bool, bool) {
	if !b.wt || len(b.faces) == 0 {
		return false, false
	}
	edges := make(map[[2]uint32]int, len(b.faces)*3)
	for _, f := range b.faces {
		for i := 0; i < 3; i++ {
			a, c := f[i], f[(i+1)%3]
			if a > c {
				a, c = c, a
			}
			edges[[2]uint32{a, c}]++
		}
	}
	for _, n := range edges {
		if n != 2 {
			return false, true
		}
	}
	return true, true
}

// emit writes central's geometry keys. bbox is the EXTENTS (max-min per axis),
// not the corner coordinates: model3d.py computes `bounds[1][i] - bounds[0][i]`
// from trimesh's (2,3) bounds array and rounds to 4 decimals, and bbox_volume is
// the product of those ALREADY-ROUNDED extents rounded to 6.
func (b *meshBuilder) emit(ext string, res *Result) {
	res.Meta["triangles"] = b.triangles
	res.Meta["vertices"] = b.vertices
	res.Meta["mesh_count"] = b.meshCount
	res.set("file_format", ext)

	if b.haveBBox {
		dims := make([]float64, 3)
		finite := true
		for i := 0; i < 3; i++ {
			d := roundTo(b.max[i]-b.min[i], 4)
			if math.IsNaN(d) || math.IsInf(d, 0) {
				finite = false // central's _round drops NaN and omits bbox entirely
				break
			}
			dims[i] = d
		}
		if finite {
			res.Meta["bbox"] = dims
			res.Meta["bbox_volume"] = roundTo(dims[0]*dims[1]*dims[2], 6)
		}
	}
	if wt, ok := b.watertight(); ok {
		res.Meta["watertight"] = wt
	}
}

// roundTo rounds to n decimal places, matching Python's round() closely enough
// for coordinate data (the two differ only on exact ties, which floating-point
// extents essentially never are).
func roundTo(f float64, n int) float64 {
	p := math.Pow(10, float64(n))
	return math.Round(f*p) / p
}

// --- STL ---------------------------------------------------------------------

// parseSTL reads either STL dialect, detected by CONTENT rather than extension:
// a .stl is just as likely to be binary as ASCII, and plenty of binary STLs
// begin with the ASCII bytes "solid" (exporters copy the word into the 80-byte
// header), so the sniff is the exact size arithmetic 84 + 50*n, which only the
// real binary layout satisfies.
func parseSTL(ctx context.Context, path string, size int64, mb *meshBuilder) error {
	f, err := os.Open(path)
	if err != nil {
		return fmt.Errorf("cannot read model: %w", err)
	}
	defer f.Close()

	mb.beginMesh()

	head := make([]byte, 84)
	n, err := io.ReadFull(f, head)
	if err != nil && err != io.ErrUnexpectedEOF && err != io.EOF {
		return fmt.Errorf("cannot read model: %w", err)
	}
	if n == 84 {
		count := int64(binary.LittleEndian.Uint32(head[80:84]))
		if 84+count*50 == size {
			return parseBinarySTL(ctx, f, int(count), mb)
		}
	}
	if _, err := f.Seek(0, io.SeekStart); err != nil {
		return fmt.Errorf("cannot read model: %w", err)
	}
	return parseASCIISTL(ctx, f, mb)
}

// parseBinarySTL reads count fixed-size 50-byte records (12-byte normal, three
// 3xfloat32 corners, 2-byte attribute) from a reader already positioned past the
// 84-byte header. The count came from the size check, so it cannot over-allocate.
func parseBinarySTL(ctx context.Context, r io.Reader, count int, mb *meshBuilder) error {
	br := bufio.NewReaderSize(r, 64<<10)
	rec := make([]byte, 50)
	for i := 0; i < count; i++ {
		if i%model3dCancelEvery == 0 {
			if err := ctx.Err(); err != nil {
				return err
			}
		}
		if _, err := io.ReadFull(br, rec); err != nil {
			return fmt.Errorf("truncated binary STL at triangle %d: %w", i, err)
		}
		var t [3][3]float64
		for v := 0; v < 3; v++ {
			off := 12 + v*12
			for c := 0; c < 3; c++ {
				bits := binary.LittleEndian.Uint32(rec[off+c*4 : off+c*4+4])
				t[v][c] = float64(math.Float32frombits(bits))
			}
		}
		if err := mb.addTriangleXYZ(t); err != nil {
			return err
		}
	}
	return nil
}

// parseASCIISTL collects `vertex x y z` lines in groups of three. Facet keywords
// are not required to be well-formed: what defines a triangle in practice is the
// vertex triple, and exporters disagree about everything else in the grammar.
func parseASCIISTL(ctx context.Context, r io.Reader, mb *meshBuilder) error {
	sc := modelScanner(r)
	var pending [3][3]float64
	held := 0
	seenSolid := false
	line := 0
	for sc.Scan() {
		line++
		if line%model3dCancelEvery == 0 {
			if err := ctx.Err(); err != nil {
				return err
			}
		}
		fields := strings.Fields(sc.Text())
		if len(fields) == 0 {
			continue
		}
		switch strings.ToLower(fields[0]) {
		case "solid":
			seenSolid = true
		case "vertex":
			if len(fields) < 4 {
				return fmt.Errorf("malformed ASCII STL: vertex on line %d has %d coordinates", line, len(fields)-1)
			}
			p, err := parseXYZ(fields[1:4])
			if err != nil {
				return fmt.Errorf("malformed ASCII STL: vertex on line %d: %w", line, err)
			}
			pending[held] = p
			held++
			if held == 3 {
				if err := mb.addTriangleXYZ(pending); err != nil {
					return err
				}
				held = 0
			}
		}
	}
	if err := sc.Err(); err != nil {
		return fmt.Errorf("cannot read ASCII STL: %w", err)
	}
	if !seenSolid && mb.triangles == 0 {
		return fmt.Errorf("not a recognisable STL (neither the binary layout nor an ASCII solid)")
	}
	return nil
}

// --- OBJ ---------------------------------------------------------------------

// parseOBJ counts `v` lines as vertices and fan-triangulates every `f` line.
// `vt`/`vn` are ignored: they are texture and normal tables, and counting them as
// geometry is the classic way to report three times too many vertices.
//
// mesh_count is 1 even for a file with several `o`/`g` groups. trimesh MAY split
// such a file into a Scene, so a heavily grouped OBJ can report a different
// mesh_count on the two sides; the counts, bbox and volume still agree, and
// guessing central's grouping rule would be less honest than the fixed 1.
func parseOBJ(ctx context.Context, path string, mb *meshBuilder) error {
	f, err := os.Open(path)
	if err != nil {
		return fmt.Errorf("cannot read model: %w", err)
	}
	defer f.Close()

	mb.beginMesh()
	sc := modelScanner(f)
	line := 0
	for sc.Scan() {
		line++
		if line%model3dCancelEvery == 0 {
			if err := ctx.Err(); err != nil {
				return err
			}
		}
		fields := strings.Fields(sc.Text())
		if len(fields) == 0 {
			continue
		}
		switch fields[0] {
		case "v":
			if len(fields) < 4 {
				return fmt.Errorf("malformed OBJ: vertex on line %d has %d coordinates", line, len(fields)-1)
			}
			p, err := parseXYZ(fields[1:4])
			if err != nil {
				return fmt.Errorf("malformed OBJ: vertex on line %d: %w", line, err)
			}
			if err := mb.addVertex(p[0], p[1], p[2]); err != nil {
				return err
			}
		case "f":
			corners := fields[1:]
			if len(corners) > model3dMaxFaceVerts {
				return fmt.Errorf("model3d guard: face on line %d has %d corners", line, len(corners))
			}
			idx := make([]int, 0, len(corners))
			for _, c := range corners {
				// "v", "v/vt", "v//vn", "v/vt/vn" — only the first field is geometry.
				spec, _, _ := strings.Cut(c, "/")
				n, err := strconv.Atoi(spec)
				if err != nil || n == 0 {
					return fmt.Errorf("malformed OBJ: face index %q on line %d", c, line)
				}
				if n < 0 {
					// OBJ allows negative indices, counted back from the most recently
					// declared vertex (-1 is the last one).
					n = mb.vertices + n
				} else {
					n-- // OBJ indices are 1-based
				}
				idx = append(idx, n)
			}
			if err := mb.addFace(idx); err != nil {
				return err
			}
		}
	}
	if err := sc.Err(); err != nil {
		return fmt.Errorf("cannot read OBJ: %w", err)
	}
	return nil
}

// --- PLY ---------------------------------------------------------------------

type plyProp struct {
	name     string
	typ      string // scalar type; empty for a list property
	list     bool
	countTyp string
	itemTyp  string
}

type plyElem struct {
	name  string
	count int
	props []plyProp
}

// parsePLY reads the ASCII header, then decodes the body in whichever encoding
// the header declared. Every property of every element is decoded in order —
// including elements we do not care about — because that is the only way to stay
// in sync with a binary stream. An unknown property TYPE is therefore a hard
// error with a clear message: falling back to "triangles = declared face count"
// would silently under-report every quad-meshed file, and a silently wrong count
// is worse than a recorded failure.
func parsePLY(ctx context.Context, path string, mb *meshBuilder) error {
	f, err := os.Open(path)
	if err != nil {
		return fmt.Errorf("cannot read model: %w", err)
	}
	defer f.Close()

	br := bufio.NewReaderSize(f, 64<<10)
	format, elems, err := readPLYHeader(br)
	if err != nil {
		return err
	}

	ps := &plyScanner{br: br}
	switch format {
	case "ascii":
		ps.ascii = true
	case "binary_little_endian":
		ps.order = binary.LittleEndian
	case "binary_big_endian":
		ps.order = binary.BigEndian
	default:
		return fmt.Errorf("unsupported PLY format %q", format)
	}

	mb.beginMesh()
	for ei := range elems {
		e := &elems[ei]
		isVertex := e.name == "vertex"
		isFace := e.name == "face" || e.name == "faces"
		for row := 0; row < e.count; row++ {
			if row%model3dCancelEvery == 0 {
				if err := ctx.Err(); err != nil {
					return err
				}
			}
			if err := ps.nextRow(); err != nil {
				return fmt.Errorf("truncated PLY body in element %q at row %d: %w", e.name, row, err)
			}
			var pos [3]float64
			var idx []int
			for _, pr := range e.props {
				if !pr.list {
					v, err := ps.value(pr.typ)
					if err != nil {
						return err
					}
					switch pr.name {
					case "x":
						pos[0] = v
					case "y":
						pos[1] = v
					case "z":
						pos[2] = v
					}
					continue
				}
				cn, err := ps.value(pr.countTyp)
				if err != nil {
					return err
				}
				n := int(cn)
				if n < 0 || n > model3dMaxFaceVerts {
					return fmt.Errorf("model3d guard: PLY list property %q declares %d entries", pr.name, n)
				}
				vals := make([]int, n)
				for k := 0; k < n; k++ {
					v, err := ps.value(pr.itemTyp)
					if err != nil {
						return err
					}
					vals[k] = int(v)
				}
				if isFace && idx == nil {
					idx = vals // the first list property of a face element is its index list
				}
			}
			if isVertex {
				if err := mb.addVertex(pos[0], pos[1], pos[2]); err != nil {
					return err
				}
			}
			if isFace && idx != nil {
				if err := mb.addFace(idx); err != nil {
					return err
				}
			}
		}
	}
	return nil
}

// readPLYHeader parses the ASCII header and leaves br positioned on the first
// body byte (which is why it reads bytes rather than using a Scanner: a Scanner
// would over-read into a binary body).
func readPLYHeader(br *bufio.Reader) (format string, elems []plyElem, err error) {
	first, err := readLineLimited(br, model3dHeaderMaxBytes)
	if err != nil || strings.TrimSpace(first) != "ply" {
		return "", nil, fmt.Errorf("not a PLY file (missing magic)")
	}
	read := len(first)
	for {
		line, err := readLineLimited(br, model3dHeaderMaxBytes)
		if err != nil {
			return "", nil, fmt.Errorf("truncated PLY header")
		}
		read += len(line) + 1
		if read > model3dHeaderMaxBytes {
			return "", nil, fmt.Errorf("model3d guard: PLY header exceeds %d bytes", model3dHeaderMaxBytes)
		}
		fields := strings.Fields(line)
		if len(fields) == 0 {
			continue
		}
		switch fields[0] {
		case "comment", "obj_info":
		case "format":
			if len(fields) < 2 {
				return "", nil, fmt.Errorf("malformed PLY format line")
			}
			format = fields[1]
		case "element":
			if len(fields) < 3 {
				return "", nil, fmt.Errorf("malformed PLY element line")
			}
			n, cerr := strconv.Atoi(fields[2])
			if cerr != nil || n < 0 || n > model3dMaxVertices {
				return "", nil, fmt.Errorf("malformed PLY element count %q", fields[2])
			}
			elems = append(elems, plyElem{name: fields[1], count: n})
		case "property":
			if len(elems) == 0 || len(fields) < 3 {
				return "", nil, fmt.Errorf("malformed PLY property line")
			}
			e := &elems[len(elems)-1]
			if fields[1] == "list" {
				if len(fields) < 5 {
					return "", nil, fmt.Errorf("malformed PLY list property line")
				}
				e.props = append(e.props, plyProp{list: true, countTyp: fields[2], itemTyp: fields[3], name: fields[4]})
				continue
			}
			e.props = append(e.props, plyProp{typ: fields[1], name: fields[2]})
		case "end_header":
			return format, elems, nil
		}
	}
}

// plyScanner reads element property values from either an ASCII or a binary PLY
// body behind one interface, so the element walk above is written once.
type plyScanner struct {
	ascii bool
	toks  []string
	pos   int

	br    *bufio.Reader
	order binary.ByteOrder
	buf   [8]byte
}

// nextRow advances to the next ASCII row; the binary body has no row framing, so
// it is a no-op there.
func (p *plyScanner) nextRow() error {
	if !p.ascii {
		return nil
	}
	for {
		line, err := readLineLimited(p.br, model3dMaxLineBytes)
		if err != nil {
			return err
		}
		p.toks = strings.Fields(line)
		p.pos = 0
		if len(p.toks) > 0 {
			return nil
		}
	}
}

// value decodes one property of the named PLY type. Everything comes back as a
// float64: the callers want either a coordinate or a small index, and one
// conversion path is far less error-prone than a typed union.
func (p *plyScanner) value(typ string) (float64, error) {
	if p.ascii {
		if p.pos >= len(p.toks) {
			return 0, fmt.Errorf("PLY row ended early (expected another %s)", typ)
		}
		tok := p.toks[p.pos]
		p.pos++
		f, err := strconv.ParseFloat(tok, 64)
		if err != nil {
			return 0, fmt.Errorf("malformed PLY value %q", tok)
		}
		return f, nil
	}
	size := plyTypeSize(typ)
	if size == 0 {
		return 0, fmt.Errorf("unsupported PLY property type %q", typ)
	}
	if _, err := io.ReadFull(p.br, p.buf[:size]); err != nil {
		return 0, fmt.Errorf("truncated PLY body: %w", err)
	}
	switch typ {
	case "char", "int8":
		return float64(int8(p.buf[0])), nil
	case "uchar", "uint8":
		return float64(p.buf[0]), nil
	case "short", "int16":
		return float64(int16(p.order.Uint16(p.buf[:2]))), nil
	case "ushort", "uint16":
		return float64(p.order.Uint16(p.buf[:2])), nil
	case "int", "int32":
		return float64(int32(p.order.Uint32(p.buf[:4]))), nil
	case "uint", "uint32":
		return float64(p.order.Uint32(p.buf[:4])), nil
	case "float", "float32":
		return float64(math.Float32frombits(p.order.Uint32(p.buf[:4]))), nil
	case "double", "float64":
		return math.Float64frombits(p.order.Uint64(p.buf[:8])), nil
	}
	return 0, fmt.Errorf("unsupported PLY property type %q", typ)
}

func plyTypeSize(typ string) int {
	switch typ {
	case "char", "int8", "uchar", "uint8":
		return 1
	case "short", "int16", "ushort", "uint16":
		return 2
	case "int", "int32", "uint", "uint32", "float", "float32":
		return 4
	case "double", "float64":
		return 8
	}
	return 0
}

// --- OFF ---------------------------------------------------------------------

// offMagics are the OFF variants whose vertex rows still begin with x y z: the
// C/N/ST prefixes only APPEND per-vertex colour, normal and texture columns, so
// taking the first three numbers stays correct. 4OFF (homogeneous 4-D vertices)
// and the "OFF BINARY" dialect are rejected explicitly rather than mis-parsed.
var offMagics = map[string]bool{
	"OFF": true, "COFF": true, "NOFF": true, "CNOFF": true,
	"NCOFF": true, "STOFF": true, "STCOFF": true, "CSTOFF": true,
}

func parseOFF(ctx context.Context, path string, mb *meshBuilder) error {
	f, err := os.Open(path)
	if err != nil {
		return fmt.Errorf("cannot read model: %w", err)
	}
	defer f.Close()

	br := bufio.NewReaderSize(f, 64<<10)
	magic, err := offNextFields(br)
	if err != nil {
		return fmt.Errorf("not an OFF file (empty)")
	}
	head := strings.ToUpper(magic[0])
	if !offMagics[head] {
		return fmt.Errorf("unsupported OFF variant %q", magic[0])
	}
	if len(magic) > 1 && strings.EqualFold(magic[1], "BINARY") {
		return fmt.Errorf("binary OFF is not supported")
	}

	// The counts usually sit on their own line, but the spec allows them on the
	// magic line; accept both rather than failing on a legal file.
	counts := magic[1:]
	if len(counts) < 3 {
		counts, err = offNextFields(br)
		if err != nil || len(counts) < 3 {
			return fmt.Errorf("malformed OFF: missing vertex/face/edge counts")
		}
	}
	nv, err1 := strconv.Atoi(counts[0])
	nf, err2 := strconv.Atoi(counts[1])
	if err1 != nil || err2 != nil || nv < 0 || nf < 0 {
		return fmt.Errorf("malformed OFF: bad vertex/face counts")
	}
	if nv > model3dMaxVertices || nf > model3dMaxTriangles {
		return fmt.Errorf("model3d guard: OFF declares %d vertices / %d faces", nv, nf)
	}

	mb.beginMesh()
	for i := 0; i < nv; i++ {
		if i%model3dCancelEvery == 0 {
			if err := ctx.Err(); err != nil {
				return err
			}
		}
		fields, err := offNextFields(br)
		if err != nil || len(fields) < 3 {
			return fmt.Errorf("malformed OFF: truncated at vertex %d", i)
		}
		p, err := parseXYZ(fields[0:3])
		if err != nil {
			return fmt.Errorf("malformed OFF: vertex %d: %w", i, err)
		}
		if err := mb.addVertex(p[0], p[1], p[2]); err != nil {
			return err
		}
	}
	for i := 0; i < nf; i++ {
		if i%model3dCancelEvery == 0 {
			if err := ctx.Err(); err != nil {
				return err
			}
		}
		fields, err := offNextFields(br)
		if err != nil || len(fields) < 1 {
			return fmt.Errorf("malformed OFF: truncated at face %d", i)
		}
		n, err := strconv.Atoi(fields[0])
		if err != nil || n < 0 || n > model3dMaxFaceVerts {
			return fmt.Errorf("malformed OFF: face %d declares %q corners", i, fields[0])
		}
		if len(fields) < n+1 {
			return fmt.Errorf("malformed OFF: face %d has %d of %d indices", i, len(fields)-1, n)
		}
		idx := make([]int, n)
		for k := 0; k < n; k++ {
			v, err := strconv.Atoi(fields[k+1])
			if err != nil {
				return fmt.Errorf("malformed OFF: face %d index %q", i, fields[k+1])
			}
			idx[k] = v
		}
		if err := mb.addFace(idx); err != nil {
			return err
		}
	}
	return nil
}

// offNextFields returns the next line's whitespace-separated tokens, skipping
// blank lines and `#` comments (legal anywhere in an OFF file).
func offNextFields(br *bufio.Reader) ([]string, error) {
	for {
		line, err := readLineLimited(br, model3dMaxLineBytes)
		if err != nil {
			return nil, err
		}
		if i := strings.IndexByte(line, '#'); i >= 0 {
			line = line[:i]
		}
		if fields := strings.Fields(line); len(fields) > 0 {
			return fields, nil
		}
	}
}

// --- glTF / GLB --------------------------------------------------------------

// gltfDoc is the slice of a glTF 2.0 asset this pass needs. Everything else
// (materials, nodes, buffers, images) is irrelevant to geometry counts.
type gltfDoc struct {
	Meshes []struct {
		Primitives []struct {
			Attributes map[string]int `json:"attributes"`
			Indices    *int           `json:"indices"`
		} `json:"primitives"`
	} `json:"meshes"`
	Accessors []struct {
		Count int       `json:"count"`
		Min   []float64 `json:"min"`
		Max   []float64 `json:"max"`
	} `json:"accessors"`
}

// parseGLTF reads counts and bounds from the asset's JSON alone. The binary
// buffers are NEVER decoded: they are the large, attacker-shaped part of the
// file, and glTF 2.0 already requires POSITION accessors to carry min/max, so
// the bounding box is available without touching a single vertex.
//
// The consequence is that `watertight` is OMITTED for these two formats. We have
// not read the topology, so we do not know — and a missing key is honest where a
// guessed one would quietly claim a fact about the mesh.
func parseGLTF(ctx context.Context, path string, glb bool, mb *meshBuilder) error {
	f, err := os.Open(path)
	if err != nil {
		return fmt.Errorf("cannot read model: %w", err)
	}
	defer f.Close()

	var raw []byte
	if glb {
		var hdr [12]byte
		if _, err := io.ReadFull(f, hdr[:]); err != nil {
			return fmt.Errorf("truncated GLB header: %w", err)
		}
		if string(hdr[0:4]) != "glTF" {
			return fmt.Errorf("not a GLB container (bad magic)")
		}
		var chunk [8]byte
		if _, err := io.ReadFull(f, chunk[:]); err != nil {
			return fmt.Errorf("truncated GLB chunk header: %w", err)
		}
		length := int64(binary.LittleEndian.Uint32(chunk[0:4]))
		if binary.LittleEndian.Uint32(chunk[4:8]) != 0x4E4F534A { // 'JSON'
			return fmt.Errorf("first GLB chunk is not JSON")
		}
		// The declared chunk length is attacker-controlled, so it is checked BEFORE
		// the allocation rather than after the read.
		if length > model3dJSONMax {
			return fmt.Errorf("model3d guard: GLB JSON chunk %d exceeds %d bytes", length, model3dJSONMax)
		}
		raw = make([]byte, length)
		if _, err := io.ReadFull(f, raw); err != nil {
			return fmt.Errorf("truncated GLB JSON chunk: %w", err)
		}
	} else {
		raw, err = io.ReadAll(io.LimitReader(f, model3dJSONMax+1))
		if err != nil {
			return fmt.Errorf("cannot read model: %w", err)
		}
		if int64(len(raw)) > model3dJSONMax {
			return fmt.Errorf("model3d guard: glTF JSON exceeds %d bytes", model3dJSONMax)
		}
	}

	var doc gltfDoc
	if err := json.Unmarshal(raw, &doc); err != nil {
		return fmt.Errorf("glTF JSON is not parseable: %w", err)
	}

	for i := range doc.Meshes {
		if err := ctx.Err(); err != nil {
			return err
		}
		mb.beginMesh()
		for _, prim := range doc.Meshes[i].Primitives {
			ai, ok := prim.Attributes["POSITION"]
			if !ok || ai < 0 || ai >= len(doc.Accessors) {
				continue // a primitive without positions carries no geometry
			}
			pos := doc.Accessors[ai]
			// Triangles come from the index accessor when the primitive is indexed,
			// and from the position count otherwise — the same arithmetic trimesh
			// applies for the default TRIANGLES mode.
			tris := pos.Count / 3
			if prim.Indices != nil {
				ii := *prim.Indices
				if ii < 0 || ii >= len(doc.Accessors) {
					return fmt.Errorf("glTF primitive references accessor %d out of %d", ii, len(doc.Accessors))
				}
				tris = doc.Accessors[ii].Count / 3
			}
			if err := mb.addCounts(pos.Count, tris); err != nil {
				return err
			}
			if len(pos.Min) >= 3 && len(pos.Max) >= 3 {
				mb.growBBox(pos.Min[0], pos.Min[1], pos.Min[2])
				mb.growBBox(pos.Max[0], pos.Max[1], pos.Max[2])
			}
		}
	}
	return nil
}

// --- 3MF ---------------------------------------------------------------------

// parse3MF reads the OPC container's model part. The zip is opened through the
// package's existing bomb guard (openGuardedZip checks the central directory
// before anything is decompressed) and the part is streamed through openMember,
// which additionally caps the decompressed bytes — a 3MF is a zip and gets the
// same treatment as a docx.
func parse3MF(ctx context.Context, path string, mb *meshBuilder) error {
	zr, err := openGuardedZip(path)
	if err != nil {
		return err
	}
	defer zr.Close()

	name, ok := find3MFModelPart(&zr.Reader)
	if !ok {
		return fmt.Errorf("3MF container has no 3D/3dmodel.model part")
	}
	rc, ok := openMember(&zr.Reader, name)
	if !ok {
		return fmt.Errorf("cannot open 3MF part %q", name)
	}
	defer rc.Close()
	return parse3MFModel(ctx, rc, mb)
}

// find3MFModelPart locates the model part. The canonical name is
// "3D/3dmodel.model", but producers vary the case and a few emit a differently
// named .model part under 3D/, so the exact match is tried first and the tolerant
// one only as a fallback.
func find3MFModelPart(zr *zip.Reader) (string, bool) {
	const canonical = "3D/3dmodel.model"
	for _, f := range zr.File {
		if f.Name == canonical {
			return f.Name, true
		}
	}
	for _, f := range zr.File {
		low := strings.ToLower(f.Name)
		if strings.HasPrefix(low, "3d/") && strings.HasSuffix(low, ".model") {
			return f.Name, true
		}
	}
	return "", false
}

// parse3MFModel walks the model XML with encoding/xml, counting <vertex> and
// <triangle> elements per <mesh>. Triangle indices are OBJECT-LOCAL, so each
// mesh records the vertex ordinal it started at and its indices are rebased onto
// the builder's global numbering.
func parse3MFModel(ctx context.Context, r io.Reader, mb *meshBuilder) error {
	dec := xml.NewDecoder(r)
	dec.Strict = false
	base := 0
	seen := 0
	for {
		if seen%model3dCancelEvery == 0 {
			if err := ctx.Err(); err != nil {
				return err
			}
		}
		seen++
		tok, err := dec.Token()
		if err == io.EOF {
			return nil
		}
		if err != nil {
			// Unlike the document body extractors, a truncated 3MF cannot degrade to
			// a partial result: half a mesh yields counts that are simply wrong.
			return fmt.Errorf("malformed 3MF model XML: %w", err)
		}
		se, ok := tok.(xml.StartElement)
		if !ok {
			continue
		}
		switch se.Name.Local {
		case "mesh":
			mb.beginMesh()
			base = mb.vertices
		case "vertex":
			p, ok := xmlFloatAttrs(se, "x", "y", "z")
			if !ok {
				return fmt.Errorf("malformed 3MF: <vertex> without numeric x/y/z")
			}
			if err := mb.addVertex(p[0], p[1], p[2]); err != nil {
				return err
			}
		case "triangle":
			v, ok := xmlIntAttrs(se, "v1", "v2", "v3")
			if !ok {
				return fmt.Errorf("malformed 3MF: <triangle> without numeric v1/v2/v3")
			}
			if err := mb.addFace([]int{base + v[0], base + v[1], base + v[2]}); err != nil {
				return err
			}
		}
	}
}

func xmlFloatAttrs(se xml.StartElement, names ...string) ([3]float64, bool) {
	var out [3]float64
	for i, n := range names {
		found := false
		for _, a := range se.Attr {
			if a.Name.Local != n {
				continue
			}
			f, err := strconv.ParseFloat(strings.TrimSpace(a.Value), 64)
			if err != nil {
				return out, false
			}
			out[i], found = f, true
			break
		}
		if !found {
			return out, false
		}
	}
	return out, true
}

func xmlIntAttrs(se xml.StartElement, names ...string) ([3]int, bool) {
	var out [3]int
	for i, n := range names {
		found := false
		for _, a := range se.Attr {
			if a.Name.Local != n {
				continue
			}
			v, err := strconv.Atoi(strings.TrimSpace(a.Value))
			if err != nil {
				return out, false
			}
			out[i], found = v, true
			break
		}
		if !found {
			return out, false
		}
	}
	return out, true
}

// --- shared text-parsing helpers ---------------------------------------------

// modelScanner returns a line scanner with the token ceiling raised to
// model3dMaxLineBytes. See that constant for why the 64 KiB default is not
// usable on real OBJ/STL files.
func modelScanner(r io.Reader) *bufio.Scanner {
	sc := bufio.NewScanner(r)
	sc.Buffer(make([]byte, 0, 64<<10), model3dMaxLineBytes)
	return sc
}

// readLineLimited reads one newline-terminated line, refusing to buffer more than
// max bytes. It exists because bufio.Reader.ReadString would happily read a whole
// 512 MiB file that contains no newline, and because the PLY/OFF paths must not
// over-read past the header into a binary body the way a Scanner would.
func readLineLimited(br *bufio.Reader, max int) (string, error) {
	var b strings.Builder
	for {
		c, err := br.ReadByte()
		if err != nil {
			if err == io.EOF && b.Len() > 0 {
				return b.String(), nil
			}
			return "", err
		}
		if c == '\n' {
			break
		}
		if b.Len() >= max {
			return "", fmt.Errorf("model3d guard: line exceeds %d bytes", max)
		}
		b.WriteByte(c)
	}
	return strings.TrimSuffix(b.String(), "\r"), nil
}

// parseXYZ converts three tokens into a position, rejecting non-finite values —
// a NaN coordinate would poison the bounding box for the whole file.
func parseXYZ(fields []string) ([3]float64, error) {
	var p [3]float64
	for i := 0; i < 3; i++ {
		f, err := strconv.ParseFloat(fields[i], 64)
		if err != nil {
			return p, fmt.Errorf("%q is not a number", fields[i])
		}
		if math.IsNaN(f) || math.IsInf(f, 0) {
			return p, fmt.Errorf("%q is not a finite coordinate", fields[i])
		}
		p[i] = f
	}
	return p, nil
}
