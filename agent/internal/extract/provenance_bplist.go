package extract

import (
	"encoding/binary"
	"unicode/utf16"
)

// decodeWhereFroms decodes macOS's kMDItemWhereFroms xattr — a binary plist
// ("bplist00") holding an array of strings [origin, referrer] (occasionally a
// bare string). This is a deliberately minimal reader: ASCII/UTF-16 strings,
// arrays, and the integer length-extension marker are all it needs; anything
// else yields ("", ""). Malformed input never panics (every index is bounds
// checked) and never matters (provenance is optional).
func decodeWhereFroms(b []byte) (origin, referrer string) {
	strs := bplistStrings(b)
	if len(strs) > 0 {
		origin = strs[0]
	}
	if len(strs) > 1 {
		referrer = strs[1]
	}
	return origin, referrer
}

// bplistStrings returns the strings of the top-level object (an array's
// members in order, or the single string itself).
func bplistStrings(b []byte) []string {
	const trailerLen = 32
	if len(b) < 8+trailerLen || string(b[:8]) != "bplist00" {
		return nil
	}
	tr := b[len(b)-trailerLen:]
	offIntSize := int(tr[6])
	refSize := int(tr[7])
	numObjects := binary.BigEndian.Uint64(tr[8:16])
	top := binary.BigEndian.Uint64(tr[16:24])
	tableOff := binary.BigEndian.Uint64(tr[24:32])
	if offIntSize < 1 || offIntSize > 8 || refSize < 1 || refSize > 8 {
		return nil
	}
	if numObjects == 0 || numObjects > 1024 || top >= numObjects {
		return nil
	}
	if tableOff >= uint64(len(b)) || tableOff+numObjects*uint64(offIntSize) > uint64(len(b)) {
		return nil
	}
	readUint := func(p []byte) uint64 {
		var v uint64
		for _, c := range p {
			v = v<<8 | uint64(c)
		}
		return v
	}
	offsets := make([]uint64, numObjects)
	for i := uint64(0); i < numObjects; i++ {
		s := tableOff + i*uint64(offIntSize)
		offsets[i] = readUint(b[s : s+uint64(offIntSize)])
	}
	// objectAt decodes the object at index i. depth guards against cycles.
	var objectAt func(i uint64, depth int) (any, bool)
	objectAt = func(i uint64, depth int) (any, bool) {
		if depth > 4 || i >= numObjects {
			return nil, false
		}
		off := offsets[i]
		if off >= uint64(len(b)) {
			return nil, false
		}
		marker := b[off]
		kind, info := marker>>4, int(marker&0x0f)
		pos := off + 1
		// length: low nibble, or 0xF => an int object follows
		readLen := func() (int, bool) {
			if info != 0x0f {
				return info, true
			}
			if pos >= uint64(len(b)) || b[pos]>>4 != 0x1 {
				return 0, false
			}
			n := 1 << (b[pos] & 0x0f)
			if n > 8 || pos+1+uint64(n) > uint64(len(b)) {
				return 0, false
			}
			v := readUint(b[pos+1 : pos+1+uint64(n)])
			pos += 1 + uint64(n)
			if v > 1<<20 {
				return 0, false
			}
			return int(v), true
		}
		switch kind {
		case 0x5: // ASCII string
			n, ok := readLen()
			if !ok || pos+uint64(n) > uint64(len(b)) {
				return nil, false
			}
			return string(b[pos : pos+uint64(n)]), true
		case 0x6: // UTF-16BE string, n = code units
			n, ok := readLen()
			if !ok || pos+uint64(n)*2 > uint64(len(b)) {
				return nil, false
			}
			units := make([]uint16, n)
			for k := 0; k < n; k++ {
				units[k] = binary.BigEndian.Uint16(b[pos+uint64(k)*2:])
			}
			return string(utf16.Decode(units)), true
		case 0xa: // array of object refs
			n, ok := readLen()
			if !ok || pos+uint64(n)*uint64(refSize) > uint64(len(b)) {
				return nil, false
			}
			out := make([]any, 0, n)
			for k := 0; k < n; k++ {
				s := pos + uint64(k)*uint64(refSize)
				ref := readUint(b[s : s+uint64(refSize)])
				v, ok := objectAt(ref, depth+1)
				if !ok {
					continue
				}
				out = append(out, v)
			}
			return out, true
		}
		return nil, false
	}
	v, ok := objectAt(top, 0)
	if !ok {
		return nil
	}
	switch t := v.(type) {
	case string:
		return []string{t}
	case []any:
		out := make([]string, 0, len(t))
		for _, e := range t {
			if s, ok := e.(string); ok {
				out = append(out, s)
			}
		}
		return out
	}
	return nil
}
