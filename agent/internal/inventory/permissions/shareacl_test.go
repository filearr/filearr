package permissions

import "testing"

const smbAccessCSV = "\"Name\",\"AccountName\",\"AccessControlType\",\"AccessRight\"\n" +
	"\"video\",\"Everyone\",\"Allow\",\"Read\"\n" +
	"\"video\",\"HOLZHUETER\\Domain Admins\",\"Allow\",\"Full\"\n" +
	"\"video\",\"XENON\\Guests\",\"Deny\",\"Change\"\n" +
	"\"\",\"\",\"\",\"\"\n"

func TestParseSmbShareAccessCSV(t *testing.T) {
	rows := ParseSmbShareAccessCSV(smbAccessCSV)
	if len(rows) != 3 {
		t.Fatalf("rows = %d, want 3 (blank row skipped): %+v", len(rows), rows)
	}
	if rows[0].Account != "Everyone" || rows[0].Right != "Read" || rows[0].Name != "video" {
		t.Fatalf("row 0 = %+v", rows[0])
	}
}

func TestShareACEsDenyFirstAndVerbs(t *testing.T) {
	aces := ShareACEs(ParseSmbShareAccessCSV(smbAccessCSV), nil, 5)
	if len(aces) != 3 {
		t.Fatalf("aces = %d", len(aces))
	}
	if aces[0].Type != TypeDeny || aces[0].Principal.Name != "XENON\\Guests" {
		t.Fatalf("deny must sort first: %+v", aces[0])
	}
	for i, a := range aces {
		if a.Source != SourceShare || a.Share != "video" || a.OrderIndex != 5+i || a.Scope != ScopeSubtree {
			t.Fatalf("ace %d tagging: %+v", i, a)
		}
	}
	// Everyone classifies to the well-known SID even without a Windows lookup.
	if aces[1].Principal.ID != "S-1-1-0" || aces[1].Principal.WellKnown != "EVERYONE" {
		t.Fatalf("Everyone principal = %+v", aces[1].Principal)
	}
	if got := ShareRightVerbs("Change"); len(got) != 4 || got[0] != "read" {
		t.Fatalf("Change verbs = %v", got)
	}
	if ShareRightVerbs("bogus") != nil {
		t.Fatal("unknown right must map to no verbs")
	}
}

func TestSharesCoveringLongestFirstCaseInsensitive(t *testing.T) {
	shares := []struct{ Name, Path string }{
		{"video", `D:\`},
		{"plates", `d:\BlueIris\plates`},
		{"other", `E:\stuff`},
	}
	got := sharesCovering(`D:\BlueIris\plates\51C71.jpg`, shares)
	if len(got) != 2 || got[0].Name != "plates" || got[1].Name != "video" {
		t.Fatalf("covering = %+v", got)
	}
	if n := len(sharesCovering(`E:\stuffed\x`, shares)); n != 0 {
		t.Fatalf("prefix must respect segment boundaries, got %d", n)
	}
}
