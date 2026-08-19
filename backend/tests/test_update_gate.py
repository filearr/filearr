"""Scheduled / held agent updates (2026-08-18): pure gate evaluation."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from filearr.policy import PolicyModel
from filearr.update_gate import (
    hold_reason,
    parse_not_before,
    parse_update_window,
    window_open,
)


def _at(y, mo, d, h, mi, tz="America/Chicago"):
    return datetime(y, mo, d, h, mi, tzinfo=ZoneInfo(tz))


def test_parse_window_forms():
    w = parse_update_window("sat,sun 02:00-05:00 America/Chicago")
    assert w.days == frozenset({5, 6}) and w.tz == "America/Chicago" and not w.wraps
    assert parse_update_window("* 01:00-04:00").days == frozenset(range(7))
    assert parse_update_window("mon-fri 22:00-04:00").wraps
    assert parse_update_window("fri-mon 00:00-06:00").days == frozenset({4, 5, 6, 0})
    assert parse_update_window("SAT 02:00-05:00").describe() == "sat 02:00-05:00"


@pytest.mark.parametrize(
    "bad",
    ["", "weekend 02:00-05:00", "sat 25:00-05:00", "sat 02:00-02:00", "sat 2-5",
     "sat 02:00-05:00 Mars/Olympus", "02:00-05:00"],
)
def test_parse_window_rejects(bad):
    with pytest.raises(ValueError):
        parse_update_window(bad)


def test_window_open_plain_and_wrapping():
    w = parse_update_window("sat,sun 02:00-05:00 America/Chicago")
    assert window_open(w, _at(2026, 8, 22, 3, 0))  # Saturday 03:00 local
    assert not window_open(w, _at(2026, 8, 22, 5, 0))  # end is exclusive
    assert not window_open(w, _at(2026, 8, 21, 3, 0))  # Friday
    # UTC input is converted into the window's zone
    assert window_open(w, datetime(2026, 8, 22, 8, 0, tzinfo=UTC))  # 03:00 CDT
    ww = parse_update_window("sat 22:00-04:00 America/Chicago")
    assert window_open(ww, _at(2026, 8, 22, 23, 0))  # Sat late
    assert window_open(ww, _at(2026, 8, 23, 3, 0))  # Sun early (wrapped)
    assert not window_open(ww, _at(2026, 8, 23, 4, 0))
    assert not window_open(ww, _at(2026, 8, 21, 23, 0))  # Fri late: Friday not a start day


def test_not_before():
    assert parse_not_before("2026-08-23T02:00:00Z") == datetime(2026, 8, 23, 2, 0, tzinfo=UTC)
    assert parse_not_before("2026-08-23T02:00+02:00").utcoffset().total_seconds() == 7200
    naive = parse_not_before("2026-08-23T02:00")
    assert naive.tzinfo is not None  # central-local
    with pytest.raises(ValueError):
        parse_not_before("next tuesday")


def test_hold_reason_precedence_and_none():
    now = _at(2026, 8, 21, 12, 0)  # Friday noon
    assert hold_reason({}, now) is None
    assert hold_reason({"auto_update": False}, now).startswith("auto_update is off")
    assert "held until" in hold_reason({"update_not_before": "2026-08-23T02:00:00-05:00"}, now)
    assert hold_reason({"update_not_before": "2026-08-01T00:00:00Z"}, now) is None  # past
    assert "outside the update window" in hold_reason(
        {"update_window": "sat,sun 02:00-05:00 America/Chicago"}, now
    )
    assert hold_reason(
        {"update_window": "sat,sun 02:00-05:00 America/Chicago"}, _at(2026, 8, 22, 3, 0)
    ) is None
    # malformed values never wedge the fleet: treated as absent
    assert hold_reason({"update_window": "garbage", "update_not_before": "??"}, now) is None


def test_policy_model_validates_keys():
    PolicyModel.model_validate({"update_window": "sat,sun 02:00-05:00"})
    PolicyModel.model_validate({"update_not_before": "2026-08-23T02:00"})
    with pytest.raises(ValidationError):
        PolicyModel.model_validate({"update_window": "sometime"})
    with pytest.raises(ValidationError):
        PolicyModel.model_validate({"update_not_before": "later"})


def test_policy_model_update_poll_interval_bounds():
    # 2026-08-19: update_poll_interval_seconds (agent-enforced) 300 s .. 7 days.
    PolicyModel.model_validate({"update_poll_interval_seconds": 300})
    PolicyModel.model_validate({"update_poll_interval_seconds": 604800})
    with pytest.raises(ValidationError):
        PolicyModel.model_validate({"update_poll_interval_seconds": 60})
    with pytest.raises(ValidationError):
        PolicyModel.model_validate({"update_poll_interval_seconds": 604801})
