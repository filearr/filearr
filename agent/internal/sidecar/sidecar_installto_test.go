package sidecar

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestInstallToWritesTokenlessDurableCopy(t *testing.T) {
	dl := t.TempDir()
	src := filepath.Join(dl, FileName)
	if err := os.WriteFile(src, []byte(`{"central_url":"https://agents.example.com","enrollment_token":"tok-1","ffmpeg_path":"C:\\ff\\ffmpeg.exe"}`), 0o600); err != nil {
		t.Fatal(err)
	}
	c, err := LoadFile(src)
	if err != nil {
		t.Fatal(err)
	}
	dst := filepath.Join(t.TempDir(), "Filearr Agent", FileName)
	got, err := c.InstallTo(dst)
	if err != nil {
		t.Fatal(err)
	}
	if !filepath.IsAbs(got) || got != dst {
		t.Fatalf("InstallTo returned %q, want the absolute dst %q", got, dst)
	}
	buf, err := os.ReadFile(dst)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(buf), "tok-1") || strings.Contains(string(buf), "enrollment_token") {
		t.Fatalf("durable copy must not carry the enrollment token: %s", buf)
	}
	for _, want := range []string{"agents.example.com", "ffmpeg_path"} {
		if !strings.Contains(string(buf), want) {
			t.Fatalf("durable copy lost %q: %s", want, buf)
		}
	}
	// The durable copy loads and yields the same central_url.
	c2, err := LoadFile(dst)
	if err != nil {
		t.Fatal(err)
	}
	if c2.CentralURL != "https://agents.example.com" {
		t.Fatalf("central_url=%q", c2.CentralURL)
	}
	// The source still holds the token for ConsumeToken to scrub.
	if c.EnrollmentToken != "tok-1" {
		t.Fatalf("source config token changed: %q", c.EnrollmentToken)
	}
}

func TestInstallToSameFileIsNoop(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, FileName)
	body := []byte(`{"central_url":"https://c.example.com","enrollment_token":"tok"}`)
	if err := os.WriteFile(p, body, 0o600); err != nil {
		t.Fatal(err)
	}
	c, err := LoadFile(p)
	if err != nil {
		t.Fatal(err)
	}
	got, err := c.InstallTo(p)
	if err != nil {
		t.Fatal(err)
	}
	if got != p {
		t.Fatalf("got %q want %q", got, p)
	}
	after, _ := os.ReadFile(p)
	if string(after) != string(body) {
		t.Fatalf("same-file InstallTo must not rewrite (token would be lost before consume): %s", after)
	}
}
