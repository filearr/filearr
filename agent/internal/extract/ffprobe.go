package extract

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"math"
	"os/exec"
	"strconv"
	"strings"
)

// ffprobeJSON is the slice of ffprobe's output we consume. Everything is typed
// loosely (json.Number / any) because ffprobe's JSON is UNTRUSTED input: a
// missing or oddly-typed field is skipped, never fatal — exactly central's
// posture in tasks/ffprobe.py.
type ffprobeJSON struct {
	Format struct {
		FormatName string `json:"format_name"`
		Duration   any    `json:"duration"`
		BitRate    any    `json:"bit_rate"`
	} `json:"format"`
	Streams []ffprobeStream `json:"streams"`
}

type ffprobeStream struct {
	CodecType      string            `json:"codec_type"`
	CodecName      string            `json:"codec_name"`
	Width          any               `json:"width"`
	Height         any               `json:"height"`
	AvgFrameRate   string            `json:"avg_frame_rate"`
	SampleRate     any               `json:"sample_rate"`
	Channels       any               `json:"channels"`
	ChannelLayout  string            `json:"channel_layout"`
	ColorPrimaries string            `json:"color_primaries"`
	ColorTransfer  string            `json:"color_transfer"`
	Tags           map[string]any    `json:"tags"`
	Disposition    map[string]any    `json:"disposition"`
	SideDataList   []ffprobeSideData `json:"side_data_list"`
}

// ffprobeSideData is one stream- or frame-level side-data entry. Only the
// fields the HDR/DV readers consume are modelled; everything else is ignored.
type ffprobeSideData struct {
	SideDataType string `json:"side_data_type"`
	// DOVI configuration record (stream level)
	DVProfile  any `json:"dv_profile"`
	DVLevel    any `json:"dv_level"`
	DVBLCompat any `json:"dv_bl_signal_compatibility_id"`
	// Content light level metadata (frame level)
	MaxContent any `json:"max_content"`
	MaxAverage any `json:"max_average"`
	// Mastering display metadata (frame level; rationals as "n/d" strings)
	RedX         string `json:"red_x"`
	RedY         string `json:"red_y"`
	MinLuminance string `json:"min_luminance"`
	MaxLuminance string `json:"max_luminance"`
}

type ffprobeFramesJSON struct {
	Frames []struct {
		SideDataList []ffprobeSideData `json:"side_data_list"`
	} `json:"frames"`
}

// runFFprobe executes ffprobe against path and returns its parsed JSON.
//
// The argv, the timeout and the output cap are a deliberate byte-for-byte mirror
// of backend/filearr/tasks/ffprobe.py probe(): same flags, same "--" terminator
// isolating the untrusted path, same 30s wall clock, same 8 MiB stdout ceiling.
// Both sides must see the same fields for the same file, and both must refuse
// the same pathological inputs.
func runFFprobe(ctx context.Context, binary, path string, opts Options) (*ffprobeJSON, error) {
	cctx, cancel := context.WithTimeout(ctx, opts.FFprobeTimeout)
	defer cancel()

	argv := []string{
		"-v", "error",
		"-hide_banner",
		"-print_format", "json",
		"-show_format",
		"-show_streams",
		"--",
		path, // untrusted; safe as a list arg (no shell interpretation)
	}
	cmd := exec.CommandContext(cctx, binary, argv...)
	// Bounded stdout: a capped buffer stops accumulating past the ceiling rather
	// than letting a pathological probe balloon the scan process's heap.
	out := &capBuffer{limit: FFprobeMaxOutputBytes}
	var stderr bytes.Buffer
	cmd.Stdout = out
	cmd.Stderr = &stderr

	if err := cmd.Run(); err != nil {
		if cctx.Err() == context.DeadlineExceeded {
			return nil, fmt.Errorf("ffprobe timed out after %s", opts.FFprobeTimeout)
		}
		if msg := lastLine(stderr.String()); msg != "" {
			return nil, fmt.Errorf("ffprobe failed: %s", msg)
		}
		return nil, fmt.Errorf("ffprobe failed: %w", err)
	}
	if out.overflowed {
		return nil, fmt.Errorf("ffprobe output too large (> %d bytes)", FFprobeMaxOutputBytes)
	}

	var data ffprobeJSON
	if err := json.Unmarshal(out.buf.Bytes(), &data); err != nil {
		return nil, fmt.Errorf("ffprobe output not valid JSON: %w", err)
	}
	return &data, nil
}

// probeVideo fills central's video vocabulary (tasks/ffprobe.py
// extract_video_tech): container,duration,bitrate,video_codec,width,height,
// resolution,frame_rate,hdr[,hdr_format,color_*],audio_codec + the audio/
// subtitle track lists.
func probeVideo(ctx context.Context, path string, opts Options, res *Result) error {
	data, err := runFFprobe(ctx, opts.FFprobePath, path, opts)
	if err != nil {
		return err
	}
	res.set("container", data.Format.FormatName)
	if d, ok := asFloat(data.Format.Duration); ok {
		res.Meta["duration"] = round3(d)
	}
	if br, ok := asInt(data.Format.BitRate); ok {
		res.Meta["bitrate"] = br
	}

	var audioTracks, subtitleTracks []map[string]any
	videoSeen := false
	for _, s := range data.Streams {
		switch s.CodecType {
		case "video":
			if videoSeen || truthy(s.Disposition["attached_pic"]) {
				continue // attached cover art is a single frame, not the main track
			}
			videoSeen = true
			res.set("video_codec", s.CodecName)
			w, wok := asInt(s.Width)
			h, hok := asInt(s.Height)
			if wok && hok {
				res.Meta["width"] = w
				res.Meta["height"] = h
				res.Meta["resolution"] = fmt.Sprintf("%dx%d", w, h)
			}
			if fr, ok := parseFPS(s.AvgFrameRate); ok && fr > 0 {
				res.Meta["frame_rate"] = fr
			}
			if hdr, format := detectHDR(s); hdr {
				res.Meta["hdr"] = true
				res.set("hdr_format", format)
				dvRecord(s, res)
				// Roadmap §11 deep probe (port of ffprobe.py probe_hdr_frames):
				// only for HDR streams, a few frames, failure ignored.
				if deep, err := probeHDRFrames(ctx, opts.FFprobePath, path, opts); err == nil {
					plus := deep.plus
					if deep.maxCLL > 0 {
						res.Meta["hdr_max_cll"] = deep.maxCLL
					}
					if deep.maxFALL > 0 {
						res.Meta["hdr_max_fall"] = deep.maxFALL
					}
					res.set("hdr_master_display", deep.masterDisplay)
					if plus && format == "HDR10" {
						res.set("hdr_format", "HDR10+")
					} else if plus && format == "Dolby Vision" {
						res.Meta["hdr10_plus"] = true
					}
				}
			}
			res.set("color_primaries", s.ColorPrimaries)
			res.set("color_transfer", s.ColorTransfer)
		case "audio":
			audioTracks = append(audioTracks, audioTrack(s))
		case "subtitle":
			subtitleTracks = append(subtitleTracks, subtitleTrack(s))
		}
	}
	if len(audioTracks) > 0 {
		res.Meta["audio_tracks"] = audioTracks
		if codec, ok := audioTracks[0]["codec"].(string); ok {
			res.Meta["audio_codec"] = codec
		}
	}
	if len(subtitleTracks) > 0 {
		res.Meta["subtitle_tracks"] = subtitleTracks
	}
	return nil
}

// probeAudio fills ONLY the technical audio keys central stores
// (duration/bitrate/samplerate/channels) plus audio_codec. It never touches
// title/artist/album/genre/year: those came from the tag reader, which is the
// authoritative source for them on both sides (central reads tags with tinytag,
// not ffprobe), and clobbering a real tag with a container guess would be worse
// than having no value.
func probeAudio(ctx context.Context, path string, opts Options, res *Result) error {
	data, err := runFFprobe(ctx, opts.FFprobePath, path, opts)
	if err != nil {
		return err
	}
	if d, ok := asFloat(data.Format.Duration); ok {
		res.Meta["duration"] = round3(d)
	}
	if br, ok := asFloat(data.Format.BitRate); ok {
		// UNIT PARITY, easy to get wrong: central's AUDIO bitrate comes from
		// tinytag and is in KILOBITS/sec, while its VIDEO bitrate comes from
		// ffprobe and is in BITS/sec. ffprobe gives us bits/sec either way, so
		// the audio path converts. Storing raw bits/sec here would make an
		// agent-scanned MP3 read as "320000 kbps" next to a central-scanned one.
		res.Meta["bitrate"] = round3(br / 1000)
	}
	for _, s := range data.Streams {
		if s.CodecType != "audio" {
			continue
		}
		res.set("audio_codec", s.CodecName)
		if sr, ok := asInt(s.SampleRate); ok {
			res.Meta["samplerate"] = sr
		}
		if ch, ok := asInt(s.Channels); ok {
			res.Meta["channels"] = ch
		}
		break // first/default audio stream, like central's convenience fields
	}
	return nil
}

func audioTrack(s ffprobeStream) map[string]any {
	t := map[string]any{}
	putStr(t, "codec", s.CodecName)
	if ch, ok := asInt(s.Channels); ok {
		t["channels"] = ch
	}
	putStr(t, "channel_layout", s.ChannelLayout)
	putStr(t, "language", tagLanguage(s.Tags))
	putStr(t, "title", tagTitle(s.Tags))
	t["default"] = truthy(s.Disposition["default"])
	return t
}

func subtitleTrack(s ffprobeStream) map[string]any {
	t := map[string]any{}
	putStr(t, "codec", s.CodecName)
	putStr(t, "language", tagLanguage(s.Tags))
	putStr(t, "title", tagTitle(s.Tags))
	t["forced"] = truthy(s.Disposition["forced"])
	t["default"] = truthy(s.Disposition["default"])
	return t
}

// detectHDR is a port of ffprobe.py _detect_hdr: best-effort HDR signalling from
// the video stream's colour metadata and side data.
func detectHDR(s ffprobeStream) (bool, string) {
	transfer := strings.ToLower(s.ColorTransfer)
	primaries := strings.ToLower(s.ColorPrimaries)
	sideTypes := map[string]bool{}
	for _, d := range s.SideDataList {
		sideTypes[strings.ToLower(d.SideDataType)] = true
	}
	if sideTypes["dovi configuration record"] || sideTypes["dolby vision"] {
		return true, "Dolby Vision"
	}
	if transfer == "arib-std-b67" {
		return true, "HLG"
	}
	if transfer == "smpte2084" || transfer == "smptest2084" {
		for t := range sideTypes {
			if strings.Contains(t, "dynamic hdr") || strings.Contains(t, "hdr10+") {
				return true, "HDR10+"
			}
		}
		return true, "HDR10"
	}
	if primaries == "bt2020" {
		return true, ""
	}
	return false, ""
}

// dvCompat maps dv_bl_signal_compatibility_id to what a non-DV display gets
// (port of ffprobe.py _DV_COMPAT).
var dvCompat = map[int64]string{1: "HDR10", 2: "SDR", 4: "HLG", 6: "Blu-ray HDR10"}

// dvRecord stores dv_profile / dv_level / dv_compat from the stream-level DOVI
// configuration record (port of ffprobe.py _dv_record).
func dvRecord(s ffprobeStream, res *Result) {
	for _, d := range s.SideDataList {
		if !strings.Contains(strings.ToLower(d.SideDataType), "dovi configuration record") {
			continue
		}
		if p, ok := asInt(d.DVProfile); ok {
			res.Meta["dv_profile"] = p
		}
		if l, ok := asInt(d.DVLevel); ok {
			res.Meta["dv_level"] = l
		}
		if c, ok := asInt(d.DVBLCompat); ok {
			res.set("dv_compat", dvCompat[c])
		}
		return
	}
}

type hdrFrameFacts struct {
	plus          bool
	maxCLL        int64
	maxFALL       int64
	masterDisplay string
}

// probeHDRFrames reads the side data of the first few video frames (bounded by
// -read_intervals %+#N) to tell HDR10+ (SMPTE ST 2094-40 dynamic metadata) from
// HDR10 and to capture MaxCLL / MaxFALL / mastering display. Port of
// ffprobe.py probe_hdr_frames; same argv shape and caps.
func probeHDRFrames(ctx context.Context, binary, path string, opts Options) (hdrFrameFacts, error) {
	cctx, cancel := context.WithTimeout(ctx, opts.FFprobeTimeout)
	defer cancel()
	argv := []string{
		"-v", "error",
		"-hide_banner",
		"-print_format", "json",
		"-select_streams", "v:0",
		"-read_intervals", "%+#6",
		"-show_entries", "frame_side_data",
		"--",
		path,
	}
	cmd := exec.CommandContext(cctx, binary, argv...)
	out := &capBuffer{limit: 4 * 1024 * 1024}
	cmd.Stdout = out
	if err := cmd.Run(); err != nil || out.overflowed {
		return hdrFrameFacts{}, fmt.Errorf("ffprobe frames: %v", err)
	}
	var data ffprobeFramesJSON
	if err := json.Unmarshal(out.buf.Bytes(), &data); err != nil {
		return hdrFrameFacts{}, err
	}
	return hdrFromFrames(data), nil
}

// hdrFromFrames folds frame side data into the HDR facts (pure; port of
// ffprobe.py hdr_from_frames).
func hdrFromFrames(data ffprobeFramesJSON) hdrFrameFacts {
	var f hdrFrameFacts
	for _, fr := range data.Frames {
		for _, d := range fr.SideDataList {
			t := strings.ToLower(d.SideDataType)
			switch {
			case strings.Contains(t, "2094-40"), strings.Contains(t, "hdr10+"), strings.Contains(t, "dynamic hdr"):
				f.plus = true
			case strings.Contains(t, "content light level"):
				if v, ok := asInt(d.MaxContent); ok {
					f.maxCLL = v
				}
				if v, ok := asInt(d.MaxAverage); ok {
					f.maxFALL = v
				}
			case strings.Contains(t, "mastering display"):
				var parts []string
				if name := primariesName(d.RedX, d.RedY); name != "" {
					parts = append(parts, name)
				}
				if mx, ok := ratio(d.MaxLuminance); ok {
					parts = append(parts, fmt.Sprintf("max %d nits", int(mx+0.5)))
				}
				if mn, ok := ratio(d.MinLuminance); ok {
					parts = append(parts, fmt.Sprintf("min %.4g nits", mn))
				}
				if len(parts) > 0 {
					f.masterDisplay = strings.Join(parts, ", ")
				}
			}
		}
	}
	return f
}

// ratio parses ffprobe's "n/d" rational strings (also accepts a plain number).
func ratio(s string) (float64, bool) {
	if s == "" {
		return 0, false
	}
	if i := strings.IndexByte(s, '/'); i > 0 {
		n, err1 := strconv.ParseFloat(s[:i], 64)
		d, err2 := strconv.ParseFloat(s[i+1:], 64)
		if err1 != nil || err2 != nil || d == 0 {
			return 0, false
		}
		return n / d, true
	}
	v, err := strconv.ParseFloat(s, 64)
	return v, err == nil
}

// primariesName names the mastering gamut from its red primary.
func primariesName(rx, ry string) string {
	x, ok1 := ratio(rx)
	y, ok2 := ratio(ry)
	if !ok1 || !ok2 {
		return ""
	}
	near := func(a, b float64) bool { return a-b < 0.01 && b-a < 0.01 }
	switch {
	case near(x, 0.708) && near(y, 0.292):
		return "BT.2020"
	case near(x, 0.680) && near(y, 0.320):
		return "P3-D65"
	case near(x, 0.640) && near(y, 0.330):
		return "BT.709"
	}
	return ""
}

// --- tolerant coercion helpers (ffprobe emits numbers as strings) -----------

func asFloat(v any) (float64, bool) {
	switch t := v.(type) {
	case float64:
		if math.IsNaN(t) || math.IsInf(t, 0) {
			return 0, false
		}
		return t, true
	case string:
		f, err := strconv.ParseFloat(strings.TrimSpace(t), 64)
		if err != nil || math.IsNaN(f) || math.IsInf(f, 0) {
			return 0, false
		}
		return f, true
	}
	return 0, false
}

func asInt(v any) (int64, bool) {
	f, ok := asFloat(v)
	if !ok {
		return 0, false
	}
	return int64(f), true
}

// parseFPS turns ffprobe's "num/den" rational into fps, rounded like central.
func parseFPS(rate string) (float64, bool) {
	num, den, found := strings.Cut(rate, "/")
	if !found {
		f, ok := asFloat(rate)
		return f, ok
	}
	n, nok := asFloat(num)
	d, dok := asFloat(den)
	if !nok || !dok || d == 0 {
		return 0, false
	}
	return round3(n / d), true
}

func round3(f float64) float64 { return math.Round(f*1000) / 1000 }

func truthy(v any) bool {
	switch t := v.(type) {
	case bool:
		return t
	case float64:
		return t != 0
	case string:
		return t != "" && t != "0"
	}
	return false
}

func putStr(m map[string]any, key, value string) {
	if v := cleanStr(value); v != "" {
		m[key] = v
	}
}

// tagLanguage mirrors ffprobe.py _lang: "und" is not a language.
func tagLanguage(tags map[string]any) string {
	for _, k := range []string{"language", "LANGUAGE"} {
		if s, ok := tags[k].(string); ok && s != "" && !strings.EqualFold(s, "und") {
			return s
		}
	}
	return ""
}

func tagTitle(tags map[string]any) string {
	for _, k := range []string{"title", "TITLE"} {
		if s, ok := tags[k].(string); ok && s != "" {
			return s
		}
	}
	return ""
}

func lastLine(s string) string {
	lines := strings.Split(strings.TrimSpace(s), "\n")
	for i := len(lines) - 1; i >= 0; i-- {
		if t := strings.TrimSpace(lines[i]); t != "" {
			return t
		}
	}
	return ""
}

// capBuffer accumulates at most limit bytes and records that it overflowed,
// discarding the excess instead of growing without bound.
type capBuffer struct {
	buf        bytes.Buffer
	limit      int
	overflowed bool
}

func (c *capBuffer) Write(p []byte) (int, error) {
	room := c.limit - c.buf.Len()
	if room <= 0 {
		c.overflowed = true
		return len(p), nil // report full consumption: the child must not see EPIPE
	}
	if len(p) > room {
		c.buf.Write(p[:room])
		c.overflowed = true
		return len(p), nil
	}
	c.buf.Write(p)
	return len(p), nil
}
