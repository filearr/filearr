package extract

import (
	"encoding/json"
	"testing"
)

// Roadmap §11 (2026-08-19): DV record + deep HDR frame probe — ports of the
// Python tests so both sides read the same facts from the same JSON.

func TestDVRecordFromStreamSideData(t *testing.T) {
	raw := `{"codec_type":"video","color_transfer":"smpte2084","side_data_list":[
	  {"side_data_type":"DOVI configuration record","dv_profile":8,"dv_level":6,
	   "rpu_present_flag":1,"el_present_flag":0,"bl_present_flag":1,"dv_bl_signal_compatibility_id":1}]}`
	var s ffprobeStream
	if err := json.Unmarshal([]byte(raw), &s); err != nil {
		t.Fatal(err)
	}
	if hdr, f := detectHDR(s); !hdr || f != "Dolby Vision" {
		t.Fatalf("detectHDR = %v %q", hdr, f)
	}
	res := &Result{Meta: map[string]any{}}
	dvRecord(s, res)
	if res.Meta["dv_profile"] != int64(8) || res.Meta["dv_level"] != int64(6) || res.Meta["dv_compat"] != "HDR10" {
		t.Fatalf("dv record = %v", res.Meta)
	}
}

func TestHDRFromFramesFoldsPlusCLLAndMastering(t *testing.T) {
	raw := `{"frames":[
	 {"side_data_list":[
	   {"side_data_type":"Mastering display metadata","red_x":"34000/50000","red_y":"16000/50000",
	    "min_luminance":"50/10000","max_luminance":"10000000/10000"},
	   {"side_data_type":"Content light level metadata","max_content":1000,"max_average":400}]},
	 {"side_data_list":[{"side_data_type":"HDR Dynamic Metadata SMPTE2094-40 (HDR10+)"}]}]}`
	var data ffprobeFramesJSON
	if err := json.Unmarshal([]byte(raw), &data); err != nil {
		t.Fatal(err)
	}
	f := hdrFromFrames(data)
	if !f.plus || f.maxCLL != 1000 || f.maxFALL != 400 {
		t.Fatalf("facts = %+v", f)
	}
	if f.masterDisplay != "P3-D65, max 1000 nits, min 0.005 nits" {
		t.Fatalf("master display = %q", f.masterDisplay)
	}
	if hdrFromFrames(ffprobeFramesJSON{}).plus {
		t.Fatal("empty frames must not be HDR10+")
	}
}
