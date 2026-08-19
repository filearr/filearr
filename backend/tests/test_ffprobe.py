"""Unit tests for the ffprobe video-metadata extractor (T1)."""

from __future__ import annotations

import subprocess

import pytest

from filearr.tasks import ffprobe
from filearr.tasks.ffprobe import FfprobeError, extract_video_tech, probe

from .conftest import requires_ffmpeg


@requires_ffmpeg
def test_happy_path_mp4(sample_mp4):
    meta = extract_video_tech(str(sample_mp4))
    assert meta["video_codec"] == "h264"
    assert meta["width"] == 320 and meta["height"] == 240
    assert meta["resolution"] == "320x240"
    assert meta["duration"] == pytest.approx(1.0, abs=0.2)
    assert meta["audio_codec"] == "aac"
    assert meta["audio_tracks"][0]["codec"] == "aac"
    assert "container" in meta


@requires_ffmpeg
def test_subtitle_and_audio_tracks_listed(sample_mkv):
    meta = extract_video_tech(str(sample_mkv))
    assert any(t["codec"] == "aac" for t in meta["audio_tracks"])
    subs = meta["subtitle_tracks"]
    assert subs and subs[0]["codec"] == "subrip"
    # frame rate parsed from ffprobe's "25/1"
    assert meta["frame_rate"] == pytest.approx(25.0)


@requires_ffmpeg
def test_corrupt_file_raises_ffprobe_error(corrupt_video):
    with pytest.raises(FfprobeError):
        extract_video_tech(str(corrupt_video))


def test_missing_binary_raises(tmp_path):
    f = tmp_path / "x.mp4"
    f.write_bytes(b"\x00")
    with pytest.raises(FfprobeError, match="not found"):
        probe(str(f), ffprobe_path="definitely-not-a-real-ffprobe-binary")


@requires_ffmpeg
def test_timeout_is_caught(monkeypatch, sample_mp4):
    """A timeout kills the child and surfaces as FfprobeError, never TimeoutExpired."""
    real_run = subprocess.run

    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(ffprobe.subprocess, "run", fake_run)
    with pytest.raises(FfprobeError, match="timed out"):
        probe(str(sample_mp4), timeout_s=0.001)
    monkeypatch.setattr(ffprobe.subprocess, "run", real_run)


@requires_ffmpeg
def test_oversized_output_rejected(monkeypatch, sample_mp4):
    with pytest.raises(FfprobeError, match="too large"):
        probe(str(sample_mp4), max_output_bytes=1)


def test_nonjson_output_rejected(monkeypatch, tmp_path):
    f = tmp_path / "x.mp4"
    f.write_bytes(b"\x00")

    class R:
        returncode = 0
        stdout = b"not json at all"
        stderr = b""

    monkeypatch.setattr(ffprobe.shutil, "which", lambda _: "/usr/bin/ffprobe")
    monkeypatch.setattr(ffprobe.subprocess, "run", lambda *a, **k: R())
    with pytest.raises(FfprobeError, match="valid JSON"):
        probe(str(f))


def test_hdr_detection_hdr10():
    stream = {
        "codec_type": "video",
        "color_transfer": "smpte2084",
        "color_primaries": "bt2020",
    }
    hdr, fmt = ffprobe._detect_hdr(stream)
    assert hdr is True and fmt == "HDR10"


def test_hdr_detection_dolby_vision():
    stream = {
        "codec_type": "video",
        "side_data_list": [{"side_data_type": "DOVI configuration record"}],
    }
    hdr, fmt = ffprobe._detect_hdr(stream)
    assert hdr is True and fmt == "Dolby Vision"


def test_sdr_not_flagged():
    hdr, fmt = ffprobe._detect_hdr({"color_transfer": "bt709", "color_primaries": "bt709"})
    assert hdr is False and fmt is None


def test_fps_parsing():
    assert ffprobe._fps("24000/1001") == pytest.approx(23.976, abs=0.001)
    assert ffprobe._fps("0/0") is None
    assert ffprobe._fps("30") == 30.0


# --- Roadmap §11 (2026-08-19): DV record + deep HDR frame probe -------------


def test_dv_record_from_stream_side_data():
    stream = {
        "color_transfer": "smpte2084",
        "side_data_list": [
            {
                "side_data_type": "DOVI configuration record",
                "dv_profile": 8,
                "dv_level": 6,
                "rpu_present_flag": 1,
                "el_present_flag": 0,
                "bl_present_flag": 1,
                "dv_bl_signal_compatibility_id": 1,
            }
        ],
    }
    assert ffprobe._detect_hdr(stream) == (True, "Dolby Vision")
    assert ffprobe._dv_record(stream) == {"dv_profile": 8, "dv_level": 6, "dv_compat": "HDR10"}
    assert ffprobe._dv_record({"side_data_list": []}) == {}


def test_hdr_from_frames_folds_plus_cll_and_mastering():
    data = {
        "frames": [
            {
                "side_data_list": [
                    {"side_data_type": "Mastering display metadata",
                     "red_x": "34000/50000", "red_y": "16000/50000",
                     "green_x": "13250/50000", "green_y": "34500/50000",
                     "blue_x": "7500/50000", "blue_y": "3000/50000",
                     "white_point_x": "15635/50000", "white_point_y": "16450/50000",
                     "min_luminance": "50/10000", "max_luminance": "10000000/10000"},
                    {"side_data_type": "Content light level metadata",
                     "max_content": 1000, "max_average": 400},
                ]
            },
            {"side_data_list": [{"side_data_type": "HDR Dynamic Metadata SMPTE2094-40 (HDR10+)"}]},
            "junk",
        ]
    }
    out = ffprobe.hdr_from_frames(data)
    assert out["hdr10_plus"] is True
    assert out["hdr_max_cll"] == 1000 and out["hdr_max_fall"] == 400
    assert out["hdr_master_display"] == "P3-D65, max 1000 nits, min 0.005 nits"
    assert ffprobe.hdr_from_frames({"frames": "x"}) == {}
    assert ffprobe.hdr_from_frames({}) == {}


def test_extract_video_tech_promotes_hdr10_plus(monkeypatch, tmp_path):
    # stream-level says HDR10; the frame probe finds dynamic metadata => HDR10+
    def fake_probe(path, **kw):
        return {
            "format": {"format_name": "matroska"},
            "streams": [
                {"codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 2160,
                 "color_transfer": "smpte2084", "color_primaries": "bt2020",
                 "avg_frame_rate": "24000/1001"}
            ],
        }

    def fake_frames(path, **kw):
        return {"hdr10_plus": True, "hdr_max_cll": 4000}

    monkeypatch.setattr(ffprobe, "probe", fake_probe)
    monkeypatch.setattr(ffprobe, "probe_hdr_frames", fake_frames)
    meta = extract_video_tech(str(tmp_path / "x.mkv"))
    assert meta["hdr"] is True and meta["hdr_format"] == "HDR10+" and meta["hdr_max_cll"] == 4000
    assert "hdr10_plus" not in meta  # folded into hdr_format, not a stray key
    # deep_hdr off => stream-level answer only, frames never probed
    called = []
    monkeypatch.setattr(ffprobe, "probe_hdr_frames", lambda *a, **k: called.append(1) or {})
    meta = extract_video_tech(str(tmp_path / "x.mkv"), deep_hdr=False)
    assert meta["hdr_format"] == "HDR10" and not called
    # a frames-probe failure never breaks the extract
    def boom(*a, **k):
        raise FfprobeError("nope")
    monkeypatch.setattr(ffprobe, "probe_hdr_frames", boom)
    assert extract_video_tech(str(tmp_path / "x.mkv"))["hdr_format"] == "HDR10"
