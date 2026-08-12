//go:build !windows && !darwin

package inventory

import "testing"

// TestParseOSRelease pins the real shapes of /etc/os-release. The parser is
// pure so the shapes can be checked without owning a machine of each
// distribution — and the shapes DO differ: Debian quotes PRETTY_NAME, Alpine
// does not, and container images built on Windows tooling have turned up with
// CRLF line endings.
func TestParseOSRelease(t *testing.T) {
	tests := []struct {
		name    string
		content string
		want    string
	}{
		{
			name:    "quoted (Debian/Ubuntu)",
			content: "NAME=\"Ubuntu\"\nVERSION_ID=\"22.04\"\nPRETTY_NAME=\"Ubuntu 22.04.4 LTS\"\n",
			want:    "Ubuntu 22.04.4 LTS",
		},
		{
			name:    "unquoted (Alpine)",
			content: "NAME=Alpine Linux\nPRETTY_NAME=Alpine Linux v3.20\n",
			want:    "Alpine Linux v3.20",
		},
		{
			name:    "CRLF and a comment",
			content: "# generated\r\nPRETTY_NAME=\"Debian GNU/Linux 13 (trixie)\"\r\n",
			want:    "Debian GNU/Linux 13 (trixie)",
		},
		{
			name:    "absent key yields empty, never a guess",
			content: "NAME=Something\n",
			want:    "",
		},
		{
			// A key whose name merely CONTAINS the one we want must not match.
			name:    "near-miss key is not matched",
			content: "X_PRETTY_NAME=\"nope\"\n",
			want:    "",
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := parseOSRelease(tc.content, "PRETTY_NAME"); got != tc.want {
				t.Fatalf("parseOSRelease = %q, want %q", got, tc.want)
			}
		})
	}
}
