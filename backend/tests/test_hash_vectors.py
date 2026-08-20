"""Known-answer digest vectors for the scan hashers (xxhash-bump tripwire).

The catalog's identity machinery (move detection, dedupe, staging verify,
agent replication) compares STORED digests against freshly computed ones, so
the hashing functions must produce byte-identical output across dependency
bumps — a silently changed digest breaks every comparison against the ~1M
already-stored rows. These vectors were generated with the real backend
functions under python-xxhash 3.8.1 and re-verified identical under 4.0.1
(libxxhash 0.8.2 -> 0.8.3); the ``quick``/``full`` columns are the SAME table
pinned in the Go mirror ``agent/internal/scan/hash_test.go`` (content
``byte[i] = (i*31 + 7) & 0xFF``), so central and agent diverging from each
other also fails one of the two suites.

If this test fails after an xxhash (or algorithm) change: do NOT update the
expected values in place. Bump ``provenance.HASH_IMPL_VERSION`` + ``_SCHEME``
so stored rows become visibly stale, let the Libraries-page hash-status prompt
drive the light per-library re-hash (``worker.rehash_library``), THEN regenerate
this table and the Go mirror together.

Sizes exercise every band: <=64 KiB whole-file, the 64-128 KiB QH-T1 band,
the ==128 KiB inclusive boundary, and >128 KiB head+tail sampling (where
``mid_hash`` first becomes non-None).
"""

from __future__ import annotations

import pytest

from filearr.tasks.extract import full_hash, mid_hash, quick_hash

# (size, quick_hash xxh3-64, mid_hash xxh3-64 | None, full_hash xxh3-128)
VECTORS = [
    (0, "2d06800538d394c2", None, "99aa06d3014798d86001c324468d497f"),
    (100, "8c97158042fbf926", None, "7f5a1f03462e52b4d61d8dbff22d515f"),
    (65536, "cf188822048798b0", None, "2c7dfeea59d29a74cf188822048798b0"),
    (65537, "d6b549a3fd1d4112", None, "a7262c585d065f55d6b549a3fd1d4112"),
    (100000, "ccf90df7e7e37036", None, "8ce7a24d31cd94b1ccf90df7e7e37036"),
    (131072, "c88c4139bb021d72", None, "3dddd29aa02945eac88c4139bb021d72"),
    (131073, "5b74c4a4515af86e", "cf188822048798b0", "32be7bc8b7759e55b23d37ad8ddf88b5"),
    (200000, "79fae598adb25419", "b14316cbe4de54f8", "09d0637de0b290ba2cece8819a5a8009"),
]


def _gen(n: int) -> bytes:
    return bytes((i * 31 + 7) & 0xFF for i in range(n))


@pytest.mark.parametrize("size,quick,mid,full", VECTORS, ids=[str(v[0]) for v in VECTORS])
def test_hash_functions_reproduce_pinned_digests(tmp_path, size, quick, mid, full):
    p = tmp_path / f"v{size}.bin"
    p.write_bytes(_gen(size))
    path = str(p)
    assert quick_hash(path, size) == quick
    assert mid_hash(path, size) == mid
    assert full_hash(path, size) == full


def test_migration_helper_matches_single_pass_hashers(tmp_path):
    """full_hashes_migration must hand back the SAME xxh3-128 as full_hash plus
    the legacy whole-file xxh3-64 — one divergent pass and the P10-T5 staging
    byte-verify starts false-failing 16-hex rows."""
    import xxhash

    from filearr.tasks.extract import full_hashes_migration

    for size in (0, 100, 131072, 200000):
        data = _gen(size)
        p = tmp_path / f"m{size}.bin"
        p.write_bytes(data)
        h128, h64 = full_hashes_migration(str(p), size)
        assert h128 == full_hash(str(p), size)
        assert h64 == xxhash.xxh3_64(data).hexdigest()
