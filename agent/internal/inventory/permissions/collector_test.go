package permissions

import (
	"context"
	"os"
	"path/filepath"
	"testing"
)

// TestCollectEmitsRecordWhereSupported: on a platform with a real read the
// collector yields ONE "permissions" entry with owner + baseline mode ACEs and a
// fidelity stamp; elsewhere it still returns the scaffold sentinel.
func TestCollectEmitsRecordWhereSupported(t *testing.T) {
	dir := t.TempDir()
	f := filepath.Join(dir, "x.txt")
	if err := os.WriteFile(f, []byte("x"), 0o640); err != nil {
		t.Fatal(err)
	}
	fi, err := os.Lstat(f)
	if err != nil {
		t.Fatal(err)
	}
	fields, err := Collector{}.Collect(context.Background(), f, fi)
	if !Supported() {
		if err == nil {
			t.Fatalf("unsupported platform must error, got %v", fields)
		}
		return
	}
	if err != nil {
		t.Fatalf("Collect: %v", err)
	}
	rec, ok := fields["permissions"].(*Record)
	if !ok || rec == nil {
		t.Fatalf("expected a *Record under \"record\", got %#v", fields)
	}
	if rec.Owner.ID == "" || rec.Fidelity == "" || len(rec.Entries) < 3 {
		t.Fatalf("incomplete record: %+v", rec)
	}
	for i, e := range rec.Entries {
		if e.OrderIndex != i {
			t.Fatalf("order index %d at position %d", e.OrderIndex, i)
		}
		if e.Type != TypeAllow && e.Type != TypeDeny {
			t.Fatalf("bad type %q", e.Type)
		}
	}
}

func TestCollectorName(t *testing.T) {
	if (Collector{}).Name() != "permissions" || CollectorName != "permissions" {
		t.Fatalf("name mismatch: %q / %q", (Collector{}).Name(), CollectorName)
	}
}
