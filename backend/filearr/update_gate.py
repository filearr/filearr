"""Scheduled / held agent updates (2026-08-18): pure evaluation of the two
policy keys that gate WHEN central offers an update, on top of ``auto_update``
(WHETHER it offers one at all).

* ``update_window`` -- ``"<days> <HH:MM>-<HH:MM>[ <IANA zone>]"``: central only
  offers updates inside the window. ``days`` is ``*`` (every day) or a comma
  list of ``mon,tue,wed,thu,fri,sat,sun`` (ranges like ``mon-fri`` allowed).
  Times are 24 h; an end earlier than the start wraps past midnight
  (``sat 22:00-04:00`` runs into Sunday morning; the day names the START).
  The optional trailing zone is an IANA name; absent = central's local zone
  (the container's ``TZ``), so ``"sat,sun 02:00-05:00"`` means the weekend
  small hours where the server lives.
* ``update_not_before`` -- ISO-8601 datetime; central answers "nothing to
  offer" until then. A naive value is central-local. "Release now" = unset it
  (or set it in the past).

Both are enforced SERVER-side on the manifest poll (like ``auto_update``), so
every agent build honours them, and both are bypassed by the operator's
per-agent update action (the click is the authorization). Everything here is
side-effect free and unit-tested against fixed clocks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

_WINDOW_RE = re.compile(
    r"^\s*(?P<days>\*|[a-z,\-]+)\s+"
    r"(?P<sh>\d{1,2}):(?P<sm>\d{2})\s*-\s*(?P<eh>\d{1,2}):(?P<em>\d{2})"
    r"(?:\s+(?P<tz>[A-Za-z_][A-Za-z0-9_+\-/]*))?\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class UpdateWindow:
    days: frozenset[int]  # 0=mon .. 6=sun, the day the window STARTS
    start: time
    end: time
    tz: str | None  # IANA name or None (= central local)

    @property
    def wraps(self) -> bool:
        return self.end <= self.start

    def describe(self) -> str:
        if len(self.days) == 7:
            days = "every day"
        else:
            days = ",".join(DAY_NAMES[d] for d in sorted(self.days))
        return f"{days} {self.start:%H:%M}-{self.end:%H:%M}" + (f" {self.tz}" if self.tz else "")


def _parse_days(spec: str) -> frozenset[int]:
    if spec == "*":
        return frozenset(range(7))
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            if a not in DAY_NAMES or b not in DAY_NAMES:
                raise ValueError(f"unknown day in range {part!r} (use mon..sun)")
            i, j = DAY_NAMES.index(a), DAY_NAMES.index(b)
            rng = range(i, j + 1) if i <= j else list(range(i, 7)) + list(range(0, j + 1))
            out.update(rng)
        else:
            if part not in DAY_NAMES:
                raise ValueError(f"unknown day {part!r} (use mon..sun or *)")
            out.add(DAY_NAMES.index(part))
    if not out:
        raise ValueError("no days given")
    return frozenset(out)


def parse_update_window(spec: str) -> UpdateWindow:
    """Parse the ``update_window`` policy string; ``ValueError`` on any defect."""
    m = _WINDOW_RE.match(spec or "")
    if not m:
        raise ValueError(
            "expected '<days> HH:MM-HH:MM [zone]', e.g. 'sat,sun 02:00-05:00' "
            "or '* 01:00-04:00 America/Chicago'"
        )
    days = _parse_days(m.group("days").lower())
    sh, sm, eh, em = (int(m.group(k)) for k in ("sh", "sm", "eh", "em"))
    for h, mi in ((sh, sm), (eh, em)):
        if not (0 <= h <= 23 and 0 <= mi <= 59):
            raise ValueError(f"time out of range: {h:02d}:{mi:02d}")
    start, end = time(sh, sm), time(eh, em)
    if start == end:
        raise ValueError("window start and end are the same minute (a zero-length window)")
    tz = m.group("tz")
    if tz:
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"unknown time zone {tz!r} (use an IANA name like Europe/Berlin)"
            ) from exc
    return UpdateWindow(days=days, start=start, end=end, tz=tz)


def _localize(now: datetime, tz: str | None) -> datetime:
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    if tz:
        return now.astimezone(ZoneInfo(tz))
    return now.astimezone()  # central's local zone (container TZ)


def window_open(window: UpdateWindow, now: datetime) -> bool:
    """True when ``now`` falls inside the window (day-of-start semantics for a
    window that wraps midnight)."""
    local = _localize(now, window.tz)
    t = local.time().replace(second=0, microsecond=0)
    wd = local.weekday()
    if not window.wraps:
        return wd in window.days and window.start <= t < window.end
    # Wraps midnight: either today is a start day and we're past start, or
    # yesterday was a start day and we're before end.
    if wd in window.days and t >= window.start:
        return True
    yesterday = (wd - 1) % 7
    return yesterday in window.days and t < window.end


def parse_not_before(value: str) -> datetime:
    """Parse ``update_not_before``: ISO-8601; naive -> central local."""
    raw = (value or "").strip()
    if not raw:
        raise ValueError("empty")
    if raw.endswith("Z") or raw.endswith("z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"not an ISO-8601 datetime: {value!r}") from exc
    if dt.tzinfo is None:
        dt = dt.astimezone()  # interpret as central local
    return dt


def hold_reason(document: dict, now: datetime | None = None) -> str | None:
    """Why central would NOT offer an update to an agent with this effective
    policy document right now, or ``None`` when nothing holds it. Order:
    ``auto_update`` off, ``update_not_before`` in the future, outside
    ``update_window``. Malformed values (which validation should have refused)
    are treated as ABSENT so a bad string can never wedge a fleet."""
    if document.get("auto_update") is False:
        return "auto_update is off for this agent's group"
    now = now or datetime.now(UTC)
    nb = document.get("update_not_before")
    if isinstance(nb, str) and nb.strip():
        try:
            when = parse_not_before(nb)
        except ValueError:
            when = None
        if when is not None and now < when:
            return f"held until {when.isoformat(timespec='minutes')} (update_not_before)"
    win = document.get("update_window")
    if isinstance(win, str) and win.strip():
        try:
            w = parse_update_window(win)
        except ValueError:
            w = None
        if w is not None and not window_open(w, now):
            return f"outside the update window ({w.describe()})"
    return None
