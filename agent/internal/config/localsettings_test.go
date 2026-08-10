package config

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

// boolp lives in policy_test.go.
func strp(s string) *string { return &s }
func intp(n int) *int       { return &n }

// A round trip must preserve every knob AND the set/absent distinction — an
// absent knob means "fall through to env/sidecar/default", which is a different
// instruction from an explicit false or zero.
func TestLocalSettingsRoundTrip(t *testing.T) {
	dir := t.TempDir()

	got, err := LoadLocalSettings(dir)
	if err != nil {
		t.Fatalf("a missing file must not be an error: %v", err)
	}
	if got.HasOverrides() || got.ScanPaused {
		t.Fatalf("a missing file must resolve to no overrides, got %+v", got)
	}

	want := LocalSettings{
		ScanPaused:          true,
		ScanCron:            strp("0 3 * * *"),
		ScanIntervalSeconds: intp(900),
		ScanOnStart:         boolp(false), // an explicit false, not "absent"
		RootsEditedAt:       "2026-08-10T12:00:00Z",
	}
	if err := SaveLocalSettings(dir, want); err != nil {
		t.Fatal(err)
	}
	got, err = LoadLocalSettings(dir)
	if err != nil {
		t.Fatal(err)
	}
	if !got.ScanPaused || got.RootsEditedAt != want.RootsEditedAt {
		t.Errorf("state did not round-trip: %+v", got)
	}
	if got.ScanCron == nil || *got.ScanCron != "0 3 * * *" {
		t.Errorf("scan_cron did not round-trip: %v", got.ScanCron)
	}
	if got.ScanIntervalSeconds == nil || *got.ScanIntervalSeconds != 900 {
		t.Errorf("scan_interval_seconds did not round-trip: %v", got.ScanIntervalSeconds)
	}
	if got.ScanOnStart == nil || *got.ScanOnStart {
		t.Errorf("an explicit false scan_on_start must survive as SET-and-false: %v", got.ScanOnStart)
	}
	if !got.HasOverrides() {
		t.Error("HasOverrides must see the schedule knobs")
	}
	if got.UpdatedAt == "" {
		t.Error("Save must stamp UpdatedAt so the health snapshot can report when it changed")
	}
}

// UpdateLocalSettings is a read-modify-write: editing the schedule must not drop
// the pause flag stored beside it (they share one file).
func TestUpdateLocalSettingsPreservesSiblings(t *testing.T) {
	dir := t.TempDir()
	if err := SaveLocalSettings(dir, LocalSettings{ScanPaused: true, ScanCron: strp("0 1 * * *")}); err != nil {
		t.Fatal(err)
	}
	got, err := UpdateLocalSettings(dir, func(ls *LocalSettings) { ls.ScanCron = nil })
	if err != nil {
		t.Fatal(err)
	}
	if !got.ScanPaused {
		t.Fatal("clearing a schedule override wiped the local pause flag")
	}
	if got.ScanCron != nil {
		t.Fatal("the clear did not take")
	}
	reloaded, _ := LoadLocalSettings(dir)
	if !reloaded.ScanPaused || reloaded.ScanCron != nil {
		t.Fatalf("persisted state disagrees with the returned one: %+v", reloaded)
	}
}

// A corrupt/truncated file must degrade to "no overrides", never fail a daemon
// start or a scan: this document gates nothing security relevant, and refusing
// to run because an operator's editor mangled it would be a worse outcome.
func TestLocalSettingsCorruptFileDegradesQuietly(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, LocalSettingsName)
	for _, blob := range []string{"", "{", "not json at all", `{"scan_cron":`, "null"} {
		if err := os.WriteFile(path, []byte(blob), 0o644); err != nil {
			t.Fatal(err)
		}
		got, err := LoadLocalSettings(dir)
		if err != nil {
			t.Fatalf("corrupt content %q must not error: %v", blob, err)
		}
		if got.HasOverrides() || got.ScanPaused {
			t.Fatalf("corrupt content %q must yield no overrides, got %+v", blob, got)
		}
	}
	// And a later good write heals the file.
	if err := SaveLocalSettings(dir, LocalSettings{ScanOnStart: boolp(true)}); err != nil {
		t.Fatal(err)
	}
	got, _ := LoadLocalSettings(dir)
	if got.ScanOnStart == nil || !*got.ScanOnStart {
		t.Fatalf("a good write after a corrupt file did not take: %+v", got)
	}
}

// The three permission accessors default to FALSE when the key is absent — a
// fleet that has never been told to delegate keeps every decision central.
func TestLocalControlPermissionsDefaultOff(t *testing.T) {
	var p Policy
	if p.LocalScanControlValue() || p.LocalScheduleControlValue() || p.LocalRootsControlValue() {
		t.Fatal("absent local_* control keys must default to false")
	}
	pol, err := ParsePolicy([]byte(`{"local_scan_control":true,"local_schedule_control":false,"local_roots_control":true}`))
	if err != nil {
		t.Fatal(err)
	}
	if !pol.LocalScanControlValue() || pol.LocalScheduleControlValue() || !pol.LocalRootsControlValue() {
		t.Fatalf("explicit values not honoured: %+v", pol)
	}
}

// LocalSurface must report which keys central EXPLICITLY set — that set is what
// locks a field in the local UI, and it has to include keys this build does not
// model (a newer central may set one before the agent understands it).
func TestLocalSurfaceReportsCentrallySetKeys(t *testing.T) {
	doc := PolicyDoc{
		Scope:   "group:nas",
		Version: 7,
		Policy:  []byte(`{"scan_cron":"0 3 * * *","local_schedule_control":true,"future_key":1}`),
	}
	ls := doc.LocalSurface(time.Date(2026, 8, 10, 12, 0, 0, 0, time.UTC), DefaultOfflineGrace)
	if !ls.ScheduleControl {
		t.Error("local_schedule_control not read")
	}
	if ls.ScanControl || ls.RootsControl {
		t.Error("unset permissions must stay off")
	}
	if !ls.CentrallySet("scan_cron") {
		t.Error("scan_cron must be reported as centrally set (and therefore locked locally)")
	}
	if !ls.CentrallySet("future_key") {
		t.Error("an unmodelled key central set must still count as centrally set")
	}
	if ls.CentrallySet("scan_interval_seconds") {
		t.Error("a key central did NOT set must stay locally editable")
	}
	if ls.Scope != "group:nas" || ls.Version != 7 {
		t.Errorf("the owning document must be identifiable: scope=%q v%d", ls.Scope, ls.Version)
	}
}
