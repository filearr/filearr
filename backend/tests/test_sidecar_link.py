"""Sidecar → parent linking algorithm (resolve_links, pure planner).

Uses lightweight fakes rather than real ORM rows so no DB is needed."""

import uuid
from dataclasses import dataclass, field

from filearr.tasks.associate import resolve_links


@dataclass
class FakeItem:
    rel_path: str
    file_category: str
    size: int = 100
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    sidecar_of: uuid.UUID | None = None


def _by_path(items):
    return {i.rel_path: i for i in items}


def test_episode_nfo_and_thumb_link_to_video():
    items = [
        FakeItem("Arcane/S01/Arcane.S01E01.mkv", "video", size=10_000),
        FakeItem("Arcane/S01/Arcane.S01E01.nfo", "other"),
        FakeItem("Arcane/S01/Arcane.S01E01-thumb.jpg", "image"),
    ]
    links = resolve_links(items)
    p = _by_path(items)
    video_id = str(p["Arcane/S01/Arcane.S01E01.mkv"].id)
    assert links[str(p["Arcane/S01/Arcane.S01E01.nfo"].id)] == video_id
    assert links[str(p["Arcane/S01/Arcane.S01E01-thumb.jpg"].id)] == video_id
    # the video itself is not a sidecar → not a key
    assert video_id not in links


def test_sidecar_seen_before_parent_still_links():
    # Order sidecars FIRST — resolve_links must not depend on ordering.
    items = [
        FakeItem("M/Dune (2021)-poster.jpg", "image"),
        FakeItem("M/Dune (2021).nfo", "other"),
        FakeItem("M/Dune (2021).mkv", "video", size=99999),
    ]
    links = resolve_links(items)
    p = _by_path(items)
    vid = str(p["M/Dune (2021).mkv"].id)
    assert links[str(p["M/Dune (2021)-poster.jpg"].id)] == vid
    assert links[str(p["M/Dune (2021).nfo"].id)] == vid


def test_directory_poster_links_to_primary_largest():
    items = [
        FakeItem("M/Dune/poster.jpg", "image"),
        FakeItem("M/Dune/Dune.mkv", "video", size=5_000_000_000),
        FakeItem("M/Dune/trailer.mkv", "video", size=100),
    ]
    links = resolve_links(items)
    p = _by_path(items)
    assert links[str(p["M/Dune/poster.jpg"].id)] == str(p["M/Dune/Dune.mkv"].id)


def test_movie_nfo_links_to_directory_primary():
    items = [
        FakeItem("M/Dune/movie.nfo", "other"),
        FakeItem("M/Dune/Dune (2021).mkv", "video", size=42),
    ]
    links = resolve_links(items)
    p = _by_path(items)
    assert links[str(p["M/Dune/movie.nfo"].id)] == str(p["M/Dune/Dune (2021).mkv"].id)


def test_unresolvable_sidecar_maps_to_none():
    items = [FakeItem("Loose/orphan.nfo", "other")]  # no parent in dir
    links = resolve_links(items)
    assert links[str(items[0].id)] is None


def test_rescan_idempotent():
    items = [
        FakeItem("M/Dune (2021).mkv", "video", size=999),
        FakeItem("M/Dune (2021).nfo", "other"),
    ]
    first = resolve_links(items)
    second = resolve_links(items)
    assert first == second


def test_sidecar_never_points_at_itself():
    # A lone .nfo directory-artwork with no primary should not self-link.
    items = [FakeItem("X/movie.nfo", "other")]
    links = resolve_links(items)
    assert links[str(items[0].id)] is None


def test_double_extension_xmp_links_to_exact_photo():
    # digiKam convention: "photo.jpg.xmp" describes "photo.jpg" — the sidecar's
    # parent_stem is "photo.jpg" while the photo indexes under stem "photo".
    # The trimmed-extension retry must find the EXACT sibling, not fall back to
    # the directory's largest image.
    items = [
        FakeItem("Pics/2024/IMG_0001.jpg", "image", size=5_000),
        FakeItem("Pics/2024/IMG_0002.jpg", "image", size=9_999_999),
        FakeItem("Pics/2024/IMG_0001.jpg.xmp", "other"),
    ]
    links = resolve_links(items)
    p = _by_path(items)
    assert links[str(p["Pics/2024/IMG_0001.jpg.xmp"].id)] == str(
        p["Pics/2024/IMG_0001.jpg"].id
    )


def test_adobe_style_xmp_still_links_same_stem():
    # Adobe convention: "photo.xmp" next to "photo.jpg" — exact stem match, the
    # pre-existing behavior, must keep working alongside the trimmed retry.
    items = [
        FakeItem("Pics/IMG_0003.jpg", "image", size=5_000),
        FakeItem("Pics/IMG_0003.xmp", "other"),
    ]
    links = resolve_links(items)
    p = _by_path(items)
    assert links[str(p["Pics/IMG_0003.xmp"].id)] == str(p["Pics/IMG_0003.jpg"].id)
