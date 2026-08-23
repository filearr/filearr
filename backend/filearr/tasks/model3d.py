"""trimesh-based geometry metadata for 3D model items.

Loads a mesh with trimesh and reports lightweight geometry facts — triangle and
vertex counts, bounding-box dimensions, and the watertight flag — without
retaining the loaded mesh. Multi-mesh scenes (a GLTF/GLB/3MF holding several
meshes) are aggregated: counts summed, bounds taken over the whole scene.

Security / reliability discipline mirrors the ffprobe extractor:
    * A hard file-size ceiling is enforced *before* handing the path to trimesh
      (trimesh reads the entire mesh into RAM, so an unbounded file is an OOM
      vector). The caller passes ``max_bytes`` from FILEARR_MODEL3D_MAX_BYTES.
    * trimesh is invoked with ``process=False`` so it does no expensive mesh
      repair/merging on untrusted geometry, and network fetches are impossible
      (we never enable trimesh's remote-resolver; a local path only).
    * Any parse failure raises Model3DError with a message safe to store; the
      caller records it under ``_extract_error`` and the job stays green.

Only formats trimesh can load as geometry are parsed: STL, OBJ, PLY, GLTF, GLB,
3MF (and OFF). STEP/STP, FBX, and BLEND have no safe pure-Python loader in
trimesh's default stack, so they are reported as ``unsupported`` rather than
parsed (still no error — just no geometry facts).

Emitted metadata schema (all keys optional; absent when unknown):
    triangles      int      total face count across all meshes
    vertices       int      total vertex count across all meshes
    mesh_count     int      number of meshes (1 for a single mesh; >1 for scenes)
    bbox           list[3]  bounding-box extents [dx, dy, dz] (source units)
    bbox_volume    float    dx*dy*dz
    watertight     bool     true only when every mesh is watertight
    file_format    str      trimesh-detected loader key, e.g. "stl", "glb"
    unsupported    bool     true when the extension has no geometry loader here
"""

from __future__ import annotations

import os
from pathlib import PurePath
from typing import Any

from filearr.humanize import human_bytes

# Extensions trimesh can load as geometry with its default, dependency-free
# stack. Deliberately excludes step/stp/fbx/blend (no safe pure loader).
_GEOMETRY_EXTS = {"stl", "obj", "ply", "off", "gltf", "glb", "3mf"}


class Model3DError(RuntimeError):
    """A 3D model could not be parsed (too large, unreadable, unloadable).
    Message is safe to store in metadata.

    ``kind`` classifies the failure for the errors surface: ``corrupt``
    (default), ``guard`` (size/geometry ceiling), ``error`` (I/O),
    ``dependency`` (a trimesh lazy-import missing from the image — a
    deployment bug, detected at the load site below).
    """

    def __init__(self, message: str, *, kind: str = "corrupt") -> None:
        super().__init__(message)
        self.kind = kind


def _round(v: Any, ndigits: int = 4) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, ndigits) if f == f else None  # drop NaN


def _iter_meshes(loaded: Any):
    """Yield every Trimesh in a loaded object (a bare mesh or a Scene)."""
    import trimesh

    if isinstance(loaded, trimesh.Trimesh):
        yield loaded
        return
    geometry = getattr(loaded, "geometry", None)
    if isinstance(geometry, dict):
        for g in geometry.values():
            if isinstance(g, trimesh.Trimesh):
                yield g


def extract_model3d(path: str, *, max_bytes: int, accurate_max_bytes: int = 0) -> dict[str, Any]:
    """Return geometry metadata for a 3D model at ``path``.

    Raises Model3DError on any failure (oversized, unreadable, unloadable) so the
    caller can record ``_extract_error``. Files whose extension has no geometry
    loader return ``{"unsupported": True}`` (not an error).

    ``accurate_max_bytes`` (roadmap §15 "accurate geometry" tier, 2026-08-19):
    when > 0 and the file is no larger than it, trimesh runs with
    ``process=True`` (vertex merge + basic repair) so a naively exported mesh
    with duplicated vertices reports a true vertex count and a correct
    ``watertight`` flag. Costlier and only for files under that smaller
    ceiling; 0 (default) keeps every file on the cheap ``process=False`` path.
    The tier used is recorded as ``geometry_tier`` ("fast" / "accurate")."""
    ext = PurePath(path).suffix.lstrip(".").lower()
    if ext not in _GEOMETRY_EXTS:
        return {"unsupported": True}

    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise Model3DError(f"cannot stat model: {exc}", kind="error") from exc
    if size > max_bytes:
        raise Model3DError(
            f"model too large ({human_bytes(size)} > {human_bytes(max_bytes)} limit)",
            kind="guard",
        )

    if ext == "3mf":
        # A 3MF is a ZIP of XML meshes: its ON-DISK size says little about the
        # parse cost — a 190 MB print bundle inflates to gigabytes of vertex
        # XML that trimesh walks in pure Python for many minutes (live
        # 2026-08-22: every worker slot pinned on 3mf files at the 300 s
        # extract timeout, and the abandoned threads kept burning CPU). Read
        # ONLY the central directory and apply the same ceiling to the
        # declared UNCOMPRESSED total (plus the bomb-ratio check), exactly as
        # documents.py does for docx/xlsx.
        from filearr.tasks.documents import DocumentError, guard_decompression

        try:
            guard_decompression(path, decompressed_max=max_bytes)
        except DocumentError as exc:
            raise Model3DError(f"3mf {exc}", kind=exc.kind) from exc

    import trimesh

    accurate = accurate_max_bytes > 0 and size <= accurate_max_bytes
    try:
        # force="mesh" would merge scenes; keep the natural type so scene bounds
        # are exact. process=False: no repair/merge work on untrusted geometry
        # (unless the opt-in accurate tier applies to this small file).
        loaded = trimesh.load(path, process=accurate)
    except Exception as exc:  # trimesh raises a zoo of exception types
        # A missing lazy-import (networkx/charset-normalizer absent from the
        # image) is a DEPLOYMENT bug, not a bad file — classify it so the
        # errors UI separates the two (live incident 2026-07-24).
        kind = "dependency" if isinstance(exc, ImportError) else "corrupt"
        raise Model3DError(f"trimesh could not load model: {exc}", kind=kind) from exc

    meshes = list(_iter_meshes(loaded))
    if not meshes:
        raise Model3DError("no mesh geometry found in file", kind="guard")

    triangles = 0
    vertices = 0
    watertight = True
    for m in meshes:
        faces = getattr(m, "faces", None)
        verts = getattr(m, "vertices", None)
        if faces is not None:
            triangles += len(faces)
        if verts is not None:
            vertices += len(verts)
        # .is_watertight can itself raise on degenerate meshes — stay defensive.
        try:
            if not bool(m.is_watertight):
                watertight = False
        except Exception:
            watertight = False

    meta: dict[str, Any] = {
        "triangles": triangles,
        "vertices": vertices,
        "mesh_count": len(meshes),
        "watertight": watertight,
        "geometry_tier": "accurate" if accurate else "fast",
    }
    if isinstance(ext, str) and ext:
        meta["file_format"] = ext

    # Scene/mesh bounds → extents. `bounds` is a (2,3) array; extents = max-min.
    bounds = getattr(loaded, "bounds", None)
    if bounds is not None:
        try:
            dims = [_round(bounds[1][i] - bounds[0][i]) for i in range(3)]
            if all(d is not None for d in dims):
                meta["bbox"] = dims
                vol = dims[0] * dims[1] * dims[2]
                meta["bbox_volume"] = round(vol, 6)
        except (IndexError, TypeError):
            pass

    return meta


# --------------------------------------------------------------------------- #
# Isolated entry point                                                         #
# --------------------------------------------------------------------------- #
def _main(argv: list[str] | None = None) -> int:
    """``python -m filearr.tasks.model3d <path> [--max-bytes N]
    [--accurate-max-bytes N]`` — run :func:`extract_model3d` in THIS process and
    print one JSON object: ``{"ok": <meta>}`` or ``{"error": {"message", "kind"}}``.

    extract.py runs every 3D file through this entry in a child process
    (``subprocess.run`` with a timeout) instead of a worker thread: trimesh's
    pure-Python loaders cannot be interrupted, so a hung parse in a thread was
    abandoned at the extract timeout yet kept spinning until the worker
    recycled — with enough of them the worker's executor starved. A child can be
    killed, and an OOM inside trimesh no longer takes the worker down."""
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(prog="filearr.tasks.model3d")
    ap.add_argument("path")
    ap.add_argument("--max-bytes", type=int, default=536_870_912)
    ap.add_argument("--accurate-max-bytes", type=int, default=0)
    ns = ap.parse_args(argv)
    try:
        meta = extract_model3d(
            ns.path, max_bytes=ns.max_bytes, accurate_max_bytes=ns.accurate_max_bytes
        )
        payload: dict[str, Any] = {"ok": meta}
    except Model3DError as exc:
        payload = {"error": {"message": str(exc), "kind": exc.kind}}
    except Exception as exc:  # noqa: BLE001 — report, never traceback to the parent
        payload = {"error": {"message": f"{type(exc).__name__}: {exc}", "kind": "error"}}
    sys.stdout.write(json.dumps(payload, default=str))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover — exercised via subprocess in tests
    raise SystemExit(_main())
