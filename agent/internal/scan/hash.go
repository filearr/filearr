package scan

import (
	"fmt"
	"io"
	"log/slog"
	"os"
	"time"

	"github.com/zeebo/xxh3"
)

// quickChunk is the head/tail window size for QuickHash: 64 KiB. Mirrors
// backend/filearr/tasks/extract.py:QUICK_CHUNK.
const quickChunk = 65536

// HashSchemeVersion identifies the BEHAVIOUR of the two hashers below, as
// opposed to their configuration. It is not used by any hashing code path and
// must never gate one — its sole consumer is internal/rehash, whose sweep
// fingerprint embeds it so that a future change to what QuickHash/FullHash
// COMPUTE automatically invalidates every agent's migration cursor and makes the
// next sweep real work again.
//
// Why the constant starts at 2 rather than 1: scheme 1 is the pre-QH-T1
// behaviour that shipped until 2026-07-18 — a fixed 64 KiB head, with the tail
// added only above 131072 bytes, so a 65537..131072-byte file had its middle and
// tail silently unhashed (the false-duplicate defect), and a 32-hex-char
// content_hash did not exist because FullHash was still xxh3-64. Scheme 2 is the
// current behaviour: whole-file quick hash at or below 131072 bytes, head+tail
// sample above it, and xxh3-128 for FullHash. Numbering the broken scheme rather
// than pretending the history starts now is what lets "hashed under scheme 2" be
// a meaningful claim.
//
// BUMP THIS whenever QuickHash or FullHash changes what it produces for the same
// bytes — a window change, an algorithm change, a boundary change. Do NOT bump it
// for a refactor that provably preserves every digest; hash_test.go's
// Python-precomputed parity fixtures are the arbiter of "provably".
const HashSchemeVersion = 2

// HashPolicy controls content-hash computation for a scan. Mirrors central's
// resolved T7 policy narrowed to the two knobs the agent needs (ruling 6): quick
// hash is ALWAYS computed; content hash only when ComputeContent AND the file is
// at/below FullMaxBytes.
type HashPolicy struct {
	ComputeContent bool
	FullMaxBytes   int64
	// Timeout bounds the WALL CLOCK spent hashing ONE file (quick + content
	// combined). 0 = unbounded (pre-2026-07-27 behavior). On network/FUSE
	// mounts a corrupt or locked file can make read(2) block forever; without a
	// bound that wedges the single-threaded walk at the same file every scan
	// (observed live: Unraid shfs, scan frozen at seen=65000 across restarts).
	// On expiry the file is left unhashed — identical to an open/read error —
	// and a WARN names the path so the operator can find the poison file.
	Timeout time.Duration
	// Log receives the timeout WARN; nil falls back to slog.Default().
	Log *slog.Logger
}

// DefaultFullMaxBytes mirrors central's global default
// FILEARR_SCAN_HASH_FULL_MAX_BYTES (config.py: scan_hash_full_max_bytes) = 1 GiB.
const DefaultFullMaxBytes int64 = 1 << 30

// DefaultHashPolicy computes both quick and content hashes with the central
// global size ceiling — the agent's local-by-construction default (no network
// quick_only downgrade, ruling 6).
func DefaultHashPolicy() HashPolicy {
	return HashPolicy{ComputeContent: true, FullMaxBytes: DefaultFullMaxBytes}
}

// QuickHash computes the xxh3_64 hex digest of a file's content as the fast
// move-detection probe. QH-T1 boundary edge (pinned identically to
// backend/filearr/tasks/extract.py:quick_hash): a file whose size <= 2*quickChunk
// (<=131072 bytes — INCLUSIVE of the 128 KiB point) is hashed IN FULL; only a
// file size > 2*quickChunk (strictly greater) is sampled as head 64 KiB + tail
// 64 KiB. The old code read a fixed 64 KiB head unconditionally and added the
// tail only above 131072, so a 64-128 KiB file had its middle+tail silently
// UNhashed (a false-duplicate defect). Byte-for-byte parity with the Python
// reference is enforced by hash_test.go against Python-precomputed digests.
// Default xxh3 seed; digest is the big-endian %016x form of Sum64 (matches
// Python xxhash .hexdigest()).
func QuickHash(pathStr string, size int64) (string, error) {
	f, err := os.Open(pathStr)
	if err != nil {
		return "", err
	}
	defer f.Close()

	h := xxh3.New()
	if size > quickChunk*2 {
		// >128 KiB: sampled head + tail (unchanged, by design).
		head := make([]byte, quickChunk)
		n, err := io.ReadFull(f, head)
		if err != nil && err != io.EOF && err != io.ErrUnexpectedEOF {
			return "", err
		}
		if _, err := h.Write(head[:n]); err != nil {
			return "", err
		}
		if _, err := f.Seek(-quickChunk, io.SeekEnd); err != nil {
			return "", err
		}
		tail := make([]byte, quickChunk)
		m, err := io.ReadFull(f, tail)
		if err != nil && err != io.EOF && err != io.ErrUnexpectedEOF {
			return "", err
		}
		if _, err := h.Write(tail[:m]); err != nil {
			return "", err
		}
	} else {
		// <=128 KiB: hash the WHOLE file. io.Copy sizes its own buffer to the data
		// (no fixed 64 KiB head cap) — cheap and correct for the small-file band.
		if _, err := io.Copy(h, f); err != nil {
			return "", err
		}
	}
	return fmt.Sprintf("%016x", h.Sum64()), nil
}

// FullHash computes the xxh3_128 hex digest (32 lowercase hex chars, big-endian
// %016x of Hi then Lo) over the whole file (QH-T3: upgraded from xxh3_64 for a
// far larger collision margin at the same throughput). io.Copy picks a buffer
// sized for the reader, avoiding the fixed 1 MiB over-allocation the brief
// measured as most of the small-file cost (§5a). Parity with extract.full_hash
// is enforced by hash_test.go.
func FullHash(pathStr string) (string, error) {
	f, err := os.Open(pathStr)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := xxh3.New128()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	sum := h.Sum128()
	return fmt.Sprintf("%016x%016x", sum.Hi, sum.Lo), nil
}

// HashFile is the exported seam over hashFile, added for internal/rehash
// (QH-T6, 2026-08-12). The migration sweep has to recompute a file's hashes with
// EXACTLY the rules a scan would apply — including the QH-T2 branch that grants
// a small file a content hash regardless of policy — and the only alternative
// was for that package to re-derive the branch from QuickHash/FullHash, which
// would make the sweep's output diverge from the scanner's the first time either
// side changed. No behaviour of its own: one call through.
//
// It exists as a wrapper rather than by exporting hashFile itself so the
// unexported name stays the one the scan package's own hot path uses, and so
// this doc comment can say who it is for.
func HashFile(pathStr string, size int64, policy HashPolicy) (quick, content string) {
	return hashFile(pathStr, size, policy)
}

// hashFile computes quick (always) and, when required, content hashes for one
// file. An OS error while hashing leaves both empty (the caller treats an
// unhashed row exactly as central does: it never matches a move and is re-queued
// by the next scan's self-heal). Mirrors move._ensure_hashes /
// extract.extract_item's hashing block.
//
// QH-T2: a file <= 2*quickChunk (128 KiB) ALWAYS gets a real content hash,
// independent of policy — it is cheap enough to hash exactly (§5a) and a sampled
// quick hash is never trustworthy identity for it. A larger file keeps the T7
// policy + ceiling gate.
func hashFile(pathStr string, size int64, policy HashPolicy) (quick, content string) {
	if policy.Timeout <= 0 {
		return hashSyncFn(pathStr, size, policy)
	}
	// Bounded: hash in a goroutine and give up at the deadline. A blocked
	// read(2) cannot be cancelled, so on timeout the goroutine (and its fd) is
	// deliberately abandoned — it either unblocks eventually and its buffered
	// send is dropped with it, or it pins one fd until process exit. Bounded by
	// the number of poison files per scan; the same tradeoff central's
	// asyncio.to_thread extract timeout makes.
	ch := make(chan [2]string, 1)
	go func() {
		q, c := hashSyncFn(pathStr, size, policy)
		ch <- [2]string{q, c}
	}()
	timer := time.NewTimer(policy.Timeout)
	defer timer.Stop()
	select {
	case r := <-ch:
		return r[0], r[1]
	case <-timer.C:
		log := policy.Log
		if log == nil {
			log = slog.Default()
		}
		log.Warn("hash timed out — file left unhashed (unreadable or hung on the underlying filesystem?)",
			"path", pathStr, "size", size, "timeout", policy.Timeout.String())
		return "", ""
	}
}

// hashSyncFn indirects the hashing body so the timeout branch is testable
// without a genuinely hung filesystem (a blocked read(2) can't be faked
// portably). Production never reassigns it.
var hashSyncFn = hashFileSync

// hashFileSync is the unbounded hashing body (see hashFile for the QH-T2
// contract it implements).
func hashFileSync(pathStr string, size int64, policy HashPolicy) (quick, content string) {
	q, err := QuickHash(pathStr, size)
	if err != nil {
		return "", ""
	}
	quick = q
	if size <= quickChunk*2 || (policy.ComputeContent && size <= policy.FullMaxBytes) {
		if c, err := FullHash(pathStr); err == nil {
			content = c
		}
	}
	return quick, content
}
