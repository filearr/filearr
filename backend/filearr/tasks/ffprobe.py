"""ffprobe-based technical metadata extraction for video items.

Invokes the system ffprobe (path from ``FILEARR_FFPROBE_PATH``) with a JSON
output format, a hard runtime timeout, and a bounded read of stdout. Filenames
are passed as an argv list (never a shell string), so no argument the file's
name might contain can inject a command. Parsing is defensive: ffprobe's JSON is
untrusted input and any missing/oddly-typed field is skipped rather than raised.

Public surface:
    probe(path)          -> raw parsed ffprobe dict (raises on failure)
    extract_video_tech(path) -> normalised metadata dict for Item.metadata_

The normalised schema (all keys optional; absent when unknown):
    container       str    format short name(s), e.g. "matroska,webm"
    duration        float  seconds
    bitrate         int    bits/sec (container-level)
    video_codec     str    e.g. "h264", "hevc", "av1"
    width           int
    height          int
    resolution      str    "WxH", e.g. "1920x1080"
    frame_rate      float  avg fps
    hdr             bool    true when HDR signalling detected
    hdr_format      str     "HDR10"/"HDR10+"/"Dolby Vision"/"HLG" when identifiable
    dv_profile      int     Dolby Vision profile (5, 7, 8, ...) from the DOVI
                            configuration record (stream-level; no frame probe)
    dv_level        int     Dolby Vision level
    dv_compat       str     DV base-layer compatibility: "HDR10" / "SDR" / "HLG" /
                            "Blu-ray HDR10" (what a non-DV display gets)
    hdr_max_cll     int     content light level (nits), from frame side data
    hdr_max_fall    int     frame-average light level (nits)
    hdr_master_display str  mastering display (e.g. "P3-D65, max 1000 nits")
    color_primaries str
    color_transfer  str
    audio_codec     str    codec of the first/default audio track (convenience)
    audio_tracks    list[dict]  {codec, channels, channel_layout, language, title, default}
    subtitle_tracks list[dict]  {codec, language, title, forced, default}
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

from filearr.humanize import human_bytes


class FfprobeError(RuntimeError):
    """ffprobe could not analyse the file (missing binary, timeout, nonzero
    exit, unparseable/oversized output). Message is safe to store in metadata."""


def _resolve_binary(ffprobe_path: str) -> str:
    """Resolve the configured ffprobe to an executable path, or raise."""
    resolved = shutil.which(ffprobe_path)
    if resolved is None:
        raise FfprobeError(f"ffprobe not found: {ffprobe_path!r}")
    return resolved


def probe(
    path: str,
    *,
    ffprobe_path: str = "ffprobe",
    timeout_s: float = 30.0,
    max_output_bytes: int = 8_388_608,
) -> dict[str, Any]:
    """Run ffprobe on ``path`` and return its parsed JSON.

    Raises FfprobeError on any failure (missing binary, timeout, nonzero exit,
    oversized or unparseable output). The child process is killed on timeout.
    """
    binary = _resolve_binary(ffprobe_path)
    argv = [
        binary,
        "-v", "error",
        "-hide_banner",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        "--",
        path,  # untrusted; safe as a list arg (no shell interpretation)
    ]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            timeout=timeout_s,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:  # child already killed by subprocess
        raise FfprobeError(f"ffprobe timed out after {timeout_s:g}s") from exc
    except OSError as exc:
        raise FfprobeError(f"ffprobe could not run: {exc}") from exc

    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        msg = detail[-1] if detail else f"exit {proc.returncode}"
        raise FfprobeError(f"ffprobe failed: {msg}")

    if len(proc.stdout) > max_output_bytes:
        raise FfprobeError(
            f"ffprobe output too large "
            f"({human_bytes(len(proc.stdout))} > {human_bytes(max_output_bytes)})"
        )

    try:
        data = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        raise FfprobeError(f"ffprobe output not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise FfprobeError("ffprobe output was not a JSON object")
    return data


# --- normalisation helpers (all tolerant of missing/odd values) --------------

def _as_float(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # drop NaN


def _as_int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _fps(rate: Any) -> float | None:
    """Parse ffprobe's 'num/den' frame-rate string into fps."""
    if not isinstance(rate, str) or "/" not in rate:
        return _as_float(rate)
    num, _, den = rate.partition("/")
    n, d = _as_float(num), _as_float(den)
    if n is None or not d:
        return None
    return round(n / d, 3)


def _lang(tags: dict[str, Any]) -> str | None:
    lang = tags.get("language") or tags.get("LANGUAGE")
    if isinstance(lang, str) and lang and lang.lower() != "und":
        return lang
    return None


def _title(tags: dict[str, Any]) -> str | None:
    t = tags.get("title") or tags.get("TITLE")
    return t if isinstance(t, str) and t else None


# DV base-layer signal compatibility ids (ETSI / Dolby "dv_bl_signal_
# compatibility_id"): what a display WITHOUT Dolby Vision decodes the file as.
_DV_COMPAT = {0: None, 1: "HDR10", 2: "SDR", 4: "HLG", 6: "Blu-ray HDR10"}


def _dv_record(stream: dict[str, Any]) -> dict[str, Any]:
    """Dolby Vision profile/level/compatibility from the stream-level DOVI
    configuration record (ffprobe exposes it in ``side_data_list`` -- no
    per-frame probe needed)."""
    for d in stream.get("side_data_list") or []:
        if not isinstance(d, dict):
            continue
        t = (d.get("side_data_type") or "").lower()
        if "dovi configuration record" not in t:
            continue
        out: dict[str, Any] = {}
        if (p := _as_int(d.get("dv_profile"))) is not None:
            out["dv_profile"] = p
        if (lv := _as_int(d.get("dv_level"))) is not None:
            out["dv_level"] = lv
        cid = _as_int(d.get("dv_bl_signal_compatibility_id"))
        if cid is not None and _DV_COMPAT.get(cid):
            out["dv_compat"] = _DV_COMPAT[cid]
        return out
    return {}


def probe_hdr_frames(
    path: str,
    *,
    ffprobe_path: str = "ffprobe",
    timeout_s: float = 30.0,
    frames: int = 6,
) -> dict[str, Any]:
    """Roadmap §11 "deep probe": read the side data of the first few video
    frames to tell **HDR10+** (SMPTE ST 2094-40 dynamic metadata) from plain
    HDR10 and to pick up the static **mastering display** / **content light
    level** values. Bounded: ``-read_intervals %+#N`` decodes only N frames, and
    it is only called when the stream-level probe already says HDR. Returns a
    (possibly empty) dict; never raises past FfprobeError."""
    binary = _resolve_binary(ffprobe_path)
    argv = [
        binary,
        "-v", "error",
        "-hide_banner",
        "-print_format", "json",
        "-select_streams", "v:0",
        "-read_intervals", f"%+#{max(1, int(frames))}",
        "-show_entries", "frame_side_data",
        "--",
        path,
    ]
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired as exc:
        raise FfprobeError(f"ffprobe (frames) timed out after {timeout_s:g}s") from exc
    except OSError as exc:
        raise FfprobeError(f"ffprobe (frames) could not run: {exc}") from exc
    if proc.returncode != 0 or len(proc.stdout) > 4_194_304:
        return {}
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return {}
    return hdr_from_frames(data)


def _nits(v: Any) -> int | None:
    """ffprobe renders luminance as a rational string like "10000000/10000" (cd/m2)."""
    if isinstance(v, str) and "/" in v:
        n, _, d = v.partition("/")
        fn, fd = _as_float(n), _as_float(d)
        if fn is None or not fd:
            return None
        return int(round(fn / fd))
    f = _as_float(v)
    return int(round(f)) if f is not None else None


def _primaries_name(d: dict[str, Any]) -> str | None:
    """Name the mastering display gamut from its red primary ("P3-D65" / "BT.2020")."""
    rx, ry = _as_float(_ratio(d.get("red_x"))), _as_float(_ratio(d.get("red_y")))
    if rx is None or ry is None:
        return None
    if abs(rx - 0.708) < 0.01 and abs(ry - 0.292) < 0.01:
        return "BT.2020"
    if abs(rx - 0.680) < 0.01 and abs(ry - 0.320) < 0.01:
        return "P3-D65"
    if abs(rx - 0.640) < 0.01 and abs(ry - 0.330) < 0.01:
        return "BT.709"
    return None


def _ratio(v: Any) -> float | None:
    if isinstance(v, str) and "/" in v:
        n, _, d = v.partition("/")
        fn, fd = _as_float(n), _as_float(d)
        return fn / fd if fn is not None and fd else None
    return _as_float(v)


def hdr_from_frames(data: dict[str, Any]) -> dict[str, Any]:
    """Pure: fold ``-show_frames`` JSON into the hdr_* keys. Exposed for tests."""
    out: dict[str, Any] = {}
    frames = data.get("frames") if isinstance(data, dict) else None
    if not isinstance(frames, list):
        return out
    for fr in frames:
        if not isinstance(fr, dict):
            continue
        for d in fr.get("side_data_list") or []:
            if not isinstance(d, dict):
                continue
            t = (d.get("side_data_type") or "").lower()
            if "2094-40" in t or "hdr10+" in t or "dynamic hdr" in t:
                out["hdr10_plus"] = True
            elif "content light level" in t:
                if (cll := _as_int(d.get("max_content"))) is not None:
                    out["hdr_max_cll"] = cll
                if (fall := _as_int(d.get("max_average"))) is not None:
                    out["hdr_max_fall"] = fall
            elif "mastering display" in t:
                parts = []
                if (name := _primaries_name(d)) is not None:
                    parts.append(name)
                mx = _nits(d.get("max_luminance"))
                mn = _ratio(d.get("min_luminance"))
                if mx is not None:
                    parts.append(f"max {mx} nits")
                if mn is not None:
                    parts.append(f"min {mn:.4g} nits")
                if parts:
                    out["hdr_master_display"] = ", ".join(parts)
            elif "dolby vision rpu" in t or "dovi rpu" in t:
                out["dv_rpu"] = True
    return out


def _detect_hdr(stream: dict[str, Any]) -> tuple[bool, str | None]:
    """Best-effort HDR detection from a video stream's colour signalling."""
    transfer = (stream.get("color_transfer") or "").lower()
    primaries = (stream.get("color_primaries") or "").lower()
    side = stream.get("side_data_list") or []
    side_types = {
        (d.get("side_data_type") or "").lower()
        for d in side
        if isinstance(d, dict)
    }

    if "dovi configuration record" in side_types or "dolby vision" in side_types:
        return True, "Dolby Vision"
    if transfer == "arib-std-b67" or primaries == "bt2020":
        # HLG uses the arib-std-b67 transfer; treat bt2020 primaries as HDR wide-gamut.
        if transfer == "arib-std-b67":
            return True, "HLG"
    if transfer in ("smpte2084", "smptest2084"):
        has_plus = any("dynamic hdr" in t or "hdr10+" in t for t in side_types)
        return True, "HDR10+" if has_plus else "HDR10"
    if primaries == "bt2020":
        return True, None
    return False, None


def extract_video_tech(
    path: str,
    *,
    ffprobe_path: str = "ffprobe",
    timeout_s: float = 30.0,
    max_output_bytes: int = 8_388_608,
    deep_hdr: bool = True,
) -> dict[str, Any]:
    """Return normalised technical metadata for a video file.

    Raises FfprobeError on probe failure so the caller can record ``_extract_error``.
    ``deep_hdr`` (roadmap §11, 2026-08-19): when the stream-level probe reports
    HDR, run the bounded first-frames probe too so HDR10+ is told apart from
    HDR10 and MaxCLL/MaxFALL/mastering display are captured. A frames-probe
    failure is swallowed (the stream-level answer stands)."""
    data = probe(
        path,
        ffprobe_path=ffprobe_path,
        timeout_s=timeout_s,
        max_output_bytes=max_output_bytes,
    )
    fmt = data.get("format") if isinstance(data.get("format"), dict) else {}
    streams = data.get("streams") if isinstance(data.get("streams"), list) else []

    meta: dict[str, Any] = {}

    container = fmt.get("format_name")
    if isinstance(container, str) and container:
        meta["container"] = container
    if (dur := _as_float(fmt.get("duration"))) is not None:
        meta["duration"] = round(dur, 3)
    if (br := _as_int(fmt.get("bit_rate"))) is not None:
        meta["bitrate"] = br

    audio_tracks: list[dict[str, Any]] = []
    subtitle_tracks: list[dict[str, Any]] = []
    video_seen = False

    for s in streams:
        if not isinstance(s, dict):
            continue
        kind = s.get("codec_type")
        tags = s.get("tags") if isinstance(s.get("tags"), dict) else {}
        disp = s.get("disposition") if isinstance(s.get("disposition"), dict) else {}

        if kind == "video" and not video_seen:
            # attached cover art / thumbnails are single frames, not the main track
            if disp.get("attached_pic"):
                continue
            video_seen = True
            if isinstance(s.get("codec_name"), str):
                meta["video_codec"] = s["codec_name"]
            w, h = _as_int(s.get("width")), _as_int(s.get("height"))
            if w is not None and h is not None:
                meta["width"] = w
                meta["height"] = h
                meta["resolution"] = f"{w}x{h}"
            if (fr := _fps(s.get("avg_frame_rate"))) is not None and fr > 0:
                meta["frame_rate"] = fr
            hdr, hdr_fmt = _detect_hdr(s)
            if hdr:
                meta["hdr"] = True
                if hdr_fmt:
                    meta["hdr_format"] = hdr_fmt
                meta.update(_dv_record(s))
                if deep_hdr:
                    try:
                        deep = probe_hdr_frames(
                            path, ffprobe_path=ffprobe_path, timeout_s=timeout_s
                        )
                    except FfprobeError:
                        deep = {}
                    plus = deep.pop("hdr10_plus", False)
                    deep.pop("dv_rpu", None)
                    meta.update(deep)
                    if plus and meta.get("hdr_format") == "HDR10":
                        meta["hdr_format"] = "HDR10+"
                    elif plus and meta.get("hdr_format") == "Dolby Vision":
                        # DV with an HDR10+ enhancement layer (rare but real)
                        meta["hdr10_plus"] = True
            for key in ("color_primaries", "color_transfer"):
                if isinstance(s.get(key), str) and s[key]:
                    meta[key] = s[key]
        elif kind == "audio":
            track = {
                k: v
                for k, v in {
                    "codec": s.get("codec_name"),
                    "channels": _as_int(s.get("channels")),
                    "channel_layout": s.get("channel_layout"),
                    "language": _lang(tags),
                    "title": _title(tags),
                    "default": bool(disp.get("default")),
                }.items()
                if v is not None
            }
            audio_tracks.append(track)
        elif kind == "subtitle":
            track = {
                k: v
                for k, v in {
                    "codec": s.get("codec_name"),
                    "language": _lang(tags),
                    "title": _title(tags),
                    "forced": bool(disp.get("forced")),
                    "default": bool(disp.get("default")),
                }.items()
                if v is not None
            }
            subtitle_tracks.append(track)

    if audio_tracks:
        meta["audio_tracks"] = audio_tracks
        first_codec = audio_tracks[0].get("codec")
        if isinstance(first_codec, str):
            meta["audio_codec"] = first_codec
    if subtitle_tracks:
        meta["subtitle_tracks"] = subtitle_tracks

    return meta
