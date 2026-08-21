"""Human-readable value formatting for user-facing messages (2026-08-21).

Guard/error messages surface verbatim in the console error report — a size
like ``1107763383`` forces the reader to count digits, so format bytes as
binary units there instead.
"""

from __future__ import annotations

_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def human_bytes(n: int | float) -> str:
    """``1107763383`` → ``'1.03 GiB'``; exact at the low end (``512 B``),
    trailing zeros trimmed (``1 GiB``, not ``1.00 GiB``)."""
    value = float(n)
    for unit in _UNITS:
        if abs(value) < 1024 or unit == _UNITS[-1]:
            if unit == "B":
                return f"{int(value)} B"
            text = f"{value:.2f}".rstrip("0").rstrip(".")
            return f"{text} {unit}"
        value /= 1024
    raise AssertionError("unreachable")
