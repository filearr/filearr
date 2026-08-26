package permissions

// Share-level ACLs (2026-08-26). Access through an SMB share is the
// INTERSECTION of the share's own ACL and the file's ACL -- Windows' Effective
// Access dialog shows "Access limited by: Share, File Permissions" -- so a
// record that only carries the file layer can call a file world-readable when
// the share (or, more often, the reverse: the share says Everyone Read while
// the NTFS ACL names only Authenticated Users) denies anonymous. The Windows
// collector therefore appends the ACEs of every local share that covers the
// path, tagged Source=SourceShare + the share name; central reconciles the
// layers. The parsing and verb mapping live here, build-tag-free, so they are
// unit-tested on every platform; only the PowerShell read is Windows-only.

import (
	"encoding/csv"
	"io"
	"sort"
	"strings"
)

// ShareAccessEntry is one row of Get-SmbShareAccess: an account, allow/deny,
// and one of the three SMB share rights.
type ShareAccessEntry struct {
	Name    string // share name
	Account string // "Everyone", "HOLZHUETER\\Domain Admins", ...
	Type    string // "Allow" | "Deny"
	Right   string // "Full" | "Change" | "Read"
}

// ParseSmbShareAccessCSV parses `Get-SmbShareAccess ... | ConvertTo-Csv
// -NoTypeInformation` output (columns Name, AccountName, AccessControlType,
// AccessRight, in any order). Malformed rows are skipped, never fatal.
func ParseSmbShareAccessCSV(text string) []ShareAccessEntry {
	r := csv.NewReader(strings.NewReader(strings.TrimPrefix(text, "\ufeff")))
	r.FieldsPerRecord = -1
	r.LazyQuotes = true
	header, err := r.Read()
	if err != nil {
		return nil
	}
	col := map[string]int{}
	for i, h := range header {
		col[strings.ToLower(strings.TrimSpace(h))] = i
	}
	get := func(rec []string, key string) string {
		i, ok := col[key]
		if !ok || i >= len(rec) {
			return ""
		}
		return strings.TrimSpace(rec[i])
	}
	var out []ShareAccessEntry
	for {
		rec, err := r.Read()
		if err == io.EOF {
			break
		}
		if err != nil {
			continue
		}
		e := ShareAccessEntry{
			Name:    get(rec, "name"),
			Account: get(rec, "accountname"),
			Type:    get(rec, "accesscontroltype"),
			Right:   get(rec, "accessright"),
		}
		if e.Account == "" || e.Right == "" {
			continue
		}
		out = append(out, e)
	}
	return out
}

// ShareRightVerbs maps an SMB share right to the normalized verb set. "Full"
// is the share-level everything (it still cannot exceed the file ACL);
// "Change" is read+write+delete; "Read" is read+execute (list/traverse).
func ShareRightVerbs(right string) []string {
	switch strings.ToLower(strings.TrimSpace(right)) {
	case "full":
		return []string{"full"}
	case "change":
		return []string{"read", "execute", "write", "delete"}
	case "read":
		return []string{"read", "execute"}
	default:
		return nil
	}
}

// ShareACEs converts the parsed rows of ONE share into ACEs (Source=share),
// resolving each account through resolve (name -> Principal; nil falls back to
// a name-only principal with well-known classification). Deny rows first, then
// allow, each group in listing order -- the canonical order the evaluator
// expects. orderBase offsets OrderIndex past the file-layer entries.
func ShareACEs(entries []ShareAccessEntry, resolve func(account string) Principal, orderBase int) []ACE {
	sorted := append([]ShareAccessEntry(nil), entries...)
	sort.SliceStable(sorted, func(i, j int) bool {
		di, dj := strings.EqualFold(sorted[i].Type, "Deny"), strings.EqualFold(sorted[j].Type, "Deny")
		return di && !dj
	})
	var out []ACE
	for _, e := range sorted {
		verbs := ShareRightVerbs(e.Right)
		if verbs == nil {
			continue
		}
		var p Principal
		if resolve != nil {
			p = resolve(e.Account)
		} else {
			p = NamePrincipal(e.Account)
		}
		typ := TypeAllow
		if strings.EqualFold(e.Type, "Deny") {
			typ = TypeDeny
		}
		out = append(out, ACE{
			Principal:  p,
			Type:       typ,
			Verbs:      verbs,
			RawMask:    "share:" + strings.ToLower(strings.TrimSpace(e.Right)),
			Scope:      ScopeSubtree, // a share right covers the whole tree below it
			Source:     SourceShare,
			Share:      e.Name,
			OrderIndex: orderBase + len(out),
		})
	}
	return out
}

// NamePrincipal builds a principal from an account NAME alone (no SID lookup):
// well-known names classify (Everyone -> S-1-1-0), anything else keeps the
// name as its id so central can still alias it.
func NamePrincipal(account string) Principal {
	name := strings.TrimSpace(account)
	short := name
	if i := strings.LastIndex(short, "\\"); i >= 0 {
		short = short[i+1:]
	}
	switch strings.ToLower(short) {
	case "everyone":
		return Principal{Kind: KindWellKnown, ID: "S-1-1-0", Name: name, WellKnown: "EVERYONE"}
	case "authenticated users":
		return Principal{Kind: KindWellKnown, ID: "S-1-5-11", Name: name, WellKnown: "AUTHENTICATED_USERS"}
	case "anonymous logon":
		return Principal{Kind: KindWellKnown, ID: "S-1-5-7", Name: name, WellKnown: "ANONYMOUS"}
	}
	p := Principal{Kind: KindGroup, ID: name, Name: name}
	if wk := ClassifyPOSIXName(short); wk != "" {
		p.WellKnown = wk
	}
	return p
}

// sharesCovering returns the shares whose exported path is the path itself
// or an ancestor of it (case-insensitive, either separator). Longest path
// first; a file under two nested shares reports both layers.
func sharesCovering(path string, shares []struct{ Name, Path string }) []struct{ Name, Path string } {
	np := normShare(path)
	var out []struct{ Name, Path string }
	for _, sh := range shares {
		root := normShare(sh.Path)
		if root == "" {
			continue
		}
		if np == root || strings.HasPrefix(np, strings.TrimSuffix(root, "/")+"/") {
			out = append(out, sh)
		}
	}
	sort.SliceStable(out, func(i, j int) bool { return len(out[i].Path) > len(out[j].Path) })
	return out
}

func normShare(p string) string {
	p = strings.ReplaceAll(strings.ToLower(strings.TrimSpace(p)), "\\", "/")
	return strings.TrimSuffix(p, "/")
}
