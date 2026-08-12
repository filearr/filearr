"""P13 — the PURE half of the unified config-group model: tier validation, the
rollout bucket function, wire-document composition and the canonical hash.

No database, no request. The layered merge itself needs rows (groups have
priorities and snapshots), so it is exercised in
``test_agent_config_groups_p13.py``; what lives here is everything that can be
pinned without one, because these are the pieces whose failure modes are silent:
a bucket function that stops being uniform, a hash that depends on dict order, a
lift precedence that quietly inverts.
"""

from __future__ import annotations

import uuid

import pytest

from filearr.agent_config import (
    LIFTED_LOCAL_KEYS,
    MAX_ROLLOUT_TIERS,
    RolloutValidationError,
    agent_bucket,
    canonical_hash,
    compose_document,
    config_etag,
    validate_tiers,
)


# --------------------------------------------------------------------------- #
# Tier validation                                                              #
# --------------------------------------------------------------------------- #
def test_validate_tiers_accepts_and_normalises():
    got = validate_tiers(
        [{"percent": 10, "delay_minutes": 0}, {"percent": 100, "delay_minutes": 60}]
    )
    assert got == [
        {"percent": 10, "delay_minutes": 0},
        {"percent": 100, "delay_minutes": 60},
    ]
    # delay_minutes defaults to 0 and extra keys are dropped (the stored list is
    # the NORMALISED one, so a stray key cannot ride along into the snapshot).
    assert validate_tiers([{"percent": 100, "note": "x"}]) == [
        {"percent": 100, "delay_minutes": 0}
    ]


def test_validate_tiers_allows_the_maximum_and_a_single_tier():
    five = [
        {"percent": 5, "delay_minutes": 0},
        {"percent": 10, "delay_minutes": 30},
        {"percent": 25, "delay_minutes": 30},
        {"percent": 50, "delay_minutes": 60},
        {"percent": 100, "delay_minutes": 120},
    ]
    assert len(validate_tiers(five)) == MAX_ROLLOUT_TIERS
    assert validate_tiers([{"percent": 100, "delay_minutes": 15}])[0]["percent"] == 100


@pytest.mark.parametrize(
    "bad",
    [
        [],  # empty
        {},  # not a list
        [{"percent": 50}],  # last is not 100
        [{"percent": 0}, {"percent": 100}],  # percent below 1
        [{"percent": 101}],  # percent above 100
        [{"percent": 50}, {"percent": 50}, {"percent": 100}],  # not strictly ascending
        [{"percent": 60}, {"percent": 30}, {"percent": 100}],  # descending
        [{"percent": 10, "delay_minutes": -1}, {"percent": 100}],  # negative delay
        [{"percent": True}],  # bool is not a percent
        ["nope"],  # not an object
        [{"percent": 10}] * 3 + [{"percent": 50}, {"percent": 90}, {"percent": 100}],
    ],
)
def test_validate_tiers_rejects(bad):
    with pytest.raises(RolloutValidationError):
        validate_tiers(bad)


def test_validate_tiers_rejects_more_than_five():
    six = [{"percent": p} for p in (1, 2, 3, 4, 5, 100)]
    with pytest.raises(RolloutValidationError):
        validate_tiers(six)


# --------------------------------------------------------------------------- #
# Bucket determinism + distribution                                            #
# --------------------------------------------------------------------------- #
def test_bucket_is_deterministic_and_in_range():
    aid = uuid.UUID("018f3c2a-0000-7000-8000-000000000001")
    first = agent_bucket(aid)
    assert first == agent_bucket(aid)  # stable across calls
    assert 0 <= first <= 99
    # And stable across PROCESSES: this is a pinned literal, not a re-derivation.
    # If this number ever changes, every fleet mid-rollout re-shuffles which
    # machines are covered — which is exactly the regression worth catching.
    assert first == 54


def test_bucket_distribution_is_roughly_uniform_over_uuidv7_like_ids():
    """A UUIDv7's time prefix is nearly constant across a burst of enrollments,
    so a naive "first byte mod 100" would put a whole afternoon's installs in one
    tier. Hashing the WHOLE id spreads them; assert that it does."""
    base = uuid.UUID("018f3c2a-0000-7000-8000-000000000000").int
    buckets = [agent_bucket(uuid.UUID(int=base + i)) for i in range(2000)]
    counts = [0] * 100
    for b in buckets:
        counts[b] += 1
    assert min(counts) > 0  # every bucket reached
    # 2000 ids over 100 buckets => 20 expected; allow generous slack, the point
    # is "no bucket holds a tenth of the fleet", not a statistical proof.
    assert max(counts) < 60


def test_bucket_10_percent_tier_selects_about_a_tenth():
    base = uuid.UUID("018f3c2a-0000-7000-8000-000000000000").int
    ids = [uuid.UUID(int=base + i) for i in range(1000)]
    covered = [a for a in ids if agent_bucket(a) < 10]
    assert 60 <= len(covered) <= 150
    # Widening the tier is strictly inclusive — an agent covered at 10% is still
    # covered at 50%, so a promotion never takes the configuration AWAY.
    covered_50 = {a for a in ids if agent_bucket(a) < 50}
    assert set(covered) <= covered_50


# --------------------------------------------------------------------------- #
# Wire-document composition (the FROZEN shape)                                 #
# --------------------------------------------------------------------------- #
def test_compose_document_shape_is_frozen():
    doc = compose_document(
        {"log_level": "debug", "scan_schedule_cron": "0 3 * * *"},
        {"watch_mode": True, "poll_interval_seconds": 120},
    )
    assert doc == {
        "watch_mode": True,
        "poll_interval_seconds": 120,
        "group": {"log_level": "debug", "scan_schedule_cron": "0 3 * * *"},
    }


def test_compose_document_lifts_local_surface_keys_from_settings():
    doc = compose_document({"web_ui_enabled": True, "auth_required": False}, {})
    assert doc["web_ui_enabled"] is True
    assert doc["auth_required"] is False
    # The lifted keys stay in the group section too — the agent reads them from
    # the top level, the console renders them from the settings section.
    assert doc["group"]["web_ui_enabled"] is True
    # local_access_enabled was not set, so nothing is invented for it.
    assert "local_access_enabled" not in doc


def test_compose_document_settings_win_the_lift_over_policy():
    doc = compose_document({"web_ui_enabled": True}, {"web_ui_enabled": False})
    assert doc["web_ui_enabled"] is True


def test_compose_document_none_settings_value_means_inherit():
    """A ``None`` in settings is "let a lower-priority group or the default supply
    this", so it must not stamp a null over a policy-supplied value."""
    doc = compose_document({"web_ui_enabled": None}, {"web_ui_enabled": True})
    assert doc["web_ui_enabled"] is True


def test_compose_document_group_section_beats_a_policy_authored_group_key():
    doc = compose_document({"log_level": "info"}, {"group": {"hand": "written"}})
    assert doc["group"] == {"log_level": "info"}


def test_compose_document_preserves_unknown_policy_keys():
    doc = compose_document({}, {"future_key": {"deep": [1, 2]}})
    assert doc["future_key"] == {"deep": [1, 2]}


def test_lifted_keys_are_the_documented_three():
    assert LIFTED_LOCAL_KEYS == (
        "web_ui_enabled",
        "local_access_enabled",
        "auth_required",
    )


# --------------------------------------------------------------------------- #
# Canonical hash + ETag                                                        #
# --------------------------------------------------------------------------- #
def test_canonical_hash_ignores_key_order():
    a = canonical_hash({"b": 1, "a": {"y": 2, "x": 1}})
    b = canonical_hash({"a": {"x": 1, "y": 2}, "b": 1})
    assert a == b
    assert len(a) == 12


def test_canonical_hash_changes_with_content():
    assert canonical_hash({"a": 1}) != canonical_hash({"a": 2})


def test_config_etag_shape():
    assert config_etag(42, "abcdef012345", 7) == '"groups/42/h:abcdef012345/t:7"'
