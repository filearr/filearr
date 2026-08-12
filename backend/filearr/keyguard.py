"""BK-T1 — key-fingerprint guard: turn a silent restore failure into a loud one.

THE INCIDENT CLASS THIS EXISTS FOR (design-backup-completeness.md, 2026-08-12).
``FILEARR_SECRET_KEY`` is the AES-GCM envelope key for alert-channel secrets
(SMTP password, webhook HMAC secret, the whole apprise URL). It is deliberately
held OUTSIDE Postgres — the threat model is a stolen dump, and a key the
database process can reach would defeat that (:mod:`filearr.alerts.crypto`).
Every consequence below follows from that one correct decision:

* a ``pg_dump`` carries the CIPHERTEXT and not the key;
* a restore onto a fresh box with a newly generated key therefore succeeds at
  every observable step — ``pg_restore`` clean, ``init_db.py`` clean, index
  rebuilt, rows all present, console green;
* and every stored channel secret is now undecryptable, permanently.

Nothing reported it. Decryption is only attempted when a channel actually
dispatches, which for most channels is "the next time something goes wrong" —
so the operator learns about it weeks later, from the alert that DIDN'T arrive.
That is the worst possible shape for a failure: invisible, delayed, and
discovered by the very mechanism it disabled.

THE FIX is to give the ciphertext a travelling companion. We stamp
``sha256(key)[:16]`` into ``instance_meta``, INSIDE the database, so it rides
the dump. On every boot we compare the fingerprint of the key the environment
supplies against the fingerprint the data was encrypted under:

    no stored fingerprint  -> STAMP it   (first run, or an instance predating
                                          this check) — silent, this is normal.
    stored == current      -> MATCH      — silent, the overwhelming case.
    stored != current      -> MISMATCH   — ERROR log + About row + /stats
                                           `degraded` entry. Never a refusal to
                                           boot: an operator may be knowingly
                                           migrating, and a guard that bricks a
                                           deployment during a disaster
                                           recovery is worse than the disease.

NEVER LOG OR RETURN THE KEY. Only ``sha256(value)[:16]`` hex leaves this module.
16 hex chars = 64 bits, which is ample to distinguish "same key" from "different
key" while being useless for recovering a high-entropy secret. It is short
enough that an operator can eyeball it against ``FILEARR_SECRET_KEY``'s
fingerprint printed by ``scripts/backup.sh`` and the bundle MANIFEST.

THE CA GETS THE SAME TREATMENT, cheaply. ``settings.ca_fingerprint`` is the
sha256 of the step-ca root DER — public pinning material central already holds,
so no probe is needed. If the ``stepca_data`` volume is lost, step-ca auto-inits
a BRAND NEW root on next start and every certificate it ever issued stops
validating; the operator's first symptom is otherwise a fleet that all fails
auth at once. Comparing the recorded root fingerprint against the configured one
names that cause immediately. Stored truncated to 16 hex for uniformity with the
secret-key row, and because the first 16 hex of the pin is exactly what an
operator sees at the head of ``step certificate fingerprint`` output.

EVERY PATH HERE IS TOTAL. This runs in the app lifespan; a check that can raise
would turn a monitoring aid into an outage. Any failure (missing table during a
pre-migration deploy window, unreachable DB, anything) degrades to
``state="unknown"`` with a reason and lets the app boot.
"""

from __future__ import annotations

import hashlib
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("filearr.keyguard")

#: ``instance_meta`` keys owned by this module.
SECRET_KEY_FP = "secret_key_fingerprint"
CA_ROOT_FP = "ca_root_fingerprint"

#: Bytes of hex kept from the sha256. 64 bits of collision resistance is far
#: more than "is this the same key", and short enough to compare by eye.
_FP_HEX = 16


def fingerprint(value: str) -> str:
    """``sha256(value)[:16]`` hex — the ONLY representation of a secret that may
    leave this process (logs, API, manifests, the About page).

    Not a password KDF on purpose, and for the same reason
    :func:`filearr.alerts.crypto.derive_key` isn't one: the input is a
    high-entropy generated token, so a slow hash buys nothing against an
    attacker who already has the fingerprint, and this is called on every boot
    plus every backup."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:_FP_HEX]


def _norm_ca(value: str) -> str:
    """Normalise a step-ca root fingerprint the way :mod:`filearr.agentcert`
    does (operators paste it with colons, in either case) and truncate to the
    common width. It is PUBLIC pinning material, so this is a display
    normalisation, not a protection."""
    return value.replace(":", "").strip().lower()[:_FP_HEX]


# --------------------------------------------------------------------------- #
# The check                                                                    #
# --------------------------------------------------------------------------- #

#: Operator-facing consequence text. Deliberately states what is BROKEN, not
#: that something "changed" — the whole failure mode is that the change looks
#: harmless. Reused verbatim by the log line, /stats and the About page so the
#: operator reads the same sentence wherever they hit it first.
SECRET_KEY_MISMATCH_REASON = (
    "FILEARR_SECRET_KEY does not match the key this database's encrypted "
    "alert-channel secrets were written under. Those secrets (SMTP passwords, "
    "webhook HMAC secrets, apprise URLs) can no longer be decrypted and their "
    "channels will fail to send. Restore the original key, or re-enter every "
    "channel secret to re-encrypt under the new one."
)

CA_ROOT_MISMATCH_REASON = (
    "The configured step-ca root fingerprint (FILEARR_CA_FINGERPRINT) is not "
    "the one this deployment recorded. If the step-ca data volume was lost, "
    "step-ca generated a NEW root and every certificate it previously issued "
    "has stopped validating — every enrolled agent needs re-enrollment."
)


async def _read(session: AsyncSession, key: str):
    from filearr.models import InstanceMeta

    return (
        await session.execute(select(InstanceMeta).where(InstanceMeta.key == key))
    ).scalar_one_or_none()


async def _check_one(
    session: AsyncSession,
    key: str,
    current: str | None,
    reason: str,
    *,
    env_var: str,
    warn_on_missing: bool = True,
) -> dict:
    """Compare one recorded fingerprint against the live one and stamp when
    absent. Returns the state dict described in :func:`check_all`.

    Order matters: the row is read, compared, and only then written. A MISMATCH
    is never overwritten — the recorded value is the evidence of what the
    ciphertext was actually written under, and silently re-stamping it would
    erase the only record that a change happened and make the warning
    self-clearing on the second boot."""
    row = await _read(session, key)
    if current is None:
        # Not configured. Not an error in itself: alert channels simply 503
        # without a key, and a stack running without agents has no CA. But if a
        # fingerprint IS recorded, this instance previously HAD one and it has
        # since vanished from the environment — which breaks exactly what a
        # WRONG value breaks, so it gets the same loud treatment.
        if row is None or not warn_on_missing:
            # ``warn_on_missing=False`` is the CA's case: turning the agents
            # profile off is a legitimate, reversible config change, and
            # shouting "your CA vanished" at an operator who simply disabled
            # agents would train them to ignore this whole surface. The
            # recorded row stays, so re-enabling with a DIFFERENT root still
            # trips the mismatch branch.
            return {"state": "unset", "recorded": row.value if row else None,
                    "current": None}
        return {
            "state": "missing",
            "recorded": row.value,
            "current": None,
            "recorded_at": row.created_at.isoformat() if row.created_at else None,
            "reason": (
                f"{env_var} is not set, but this deployment recorded "
                f"{row.value}. {reason}"
            ),
        }
    if row is None:
        from filearr.models import InstanceMeta

        session.add(InstanceMeta(key=key, value=current))
        await session.commit()
        log.info("keyguard: stamped %s=%s (first run for this database)", key, current)
        return {"state": "stamped", "recorded": current, "current": current}
    if row.value == current:
        return {
            "state": "match",
            "recorded": row.value,
            "current": current,
            "recorded_at": row.created_at.isoformat() if row.created_at else None,
        }
    return {
        "state": "mismatch",
        "recorded": row.value,
        "current": current,
        "recorded_at": row.created_at.isoformat() if row.created_at else None,
        "reason": reason,
    }


async def check_all(session: AsyncSession, settings=None) -> dict:
    """Run every fingerprint check; return ``{"secret_key": {...}, "ca_root": {...}}``.

    Each sub-dict carries ``state`` — one of:

    ``unset``     the thing isn't configured and never was (silent)
    ``stamped``   nothing was recorded; the current value has just been recorded
    ``match``     recorded == current (silent; the normal case)
    ``mismatch``  recorded != current — the loud one, carries ``reason``
    ``missing``   a fingerprint is recorded but the value is no longer configured
    ``unknown``   the check itself could not run, carries ``reason``

    plus ``recorded``/``current`` fingerprints (never values) and, when a row
    exists, ``recorded_at``. Total: any exception becomes ``unknown``."""
    from filearr.config import get_settings

    settings = settings or get_settings()
    out: dict[str, dict] = {}
    try:
        out["secret_key"] = await _check_one(
            session,
            SECRET_KEY_FP,
            fingerprint(settings.secret_key) if settings.secret_key else None,
            SECRET_KEY_MISMATCH_REASON,
            env_var="FILEARR_SECRET_KEY",
        )
    except Exception as exc:  # noqa: BLE001 — a boot-time aid must never break boot
        await _rollback(session)
        out["secret_key"] = {"state": "unknown", "reason": _reason(exc)}
    try:
        ca = getattr(settings, "ca_fingerprint", "") or ""
        out["ca_root"] = await _check_one(
            session,
            CA_ROOT_FP,
            _norm_ca(ca) if ca else None,
            CA_ROOT_MISMATCH_REASON,
            env_var="FILEARR_CA_FINGERPRINT",
            warn_on_missing=False,
        )
    except Exception as exc:  # noqa: BLE001
        await _rollback(session)
        out["ca_root"] = {"state": "unknown", "reason": _reason(exc)}
    return out


def _reason(exc: BaseException) -> str:
    text = str(exc).strip()
    # Include the type: a missing table (pre-migration deploy window, which
    # self-heals) and a refused connection send an operator to different places.
    return f"{type(exc).__name__}: {text}"[:200] if text else type(exc).__name__


async def _rollback(session: AsyncSession) -> None:
    try:
        await session.rollback()
    except Exception:  # noqa: BLE001 — best effort
        log.debug("keyguard: rollback after a failed check also failed", exc_info=True)


#: Process-local cache of the last check, so /stats and the About page can
#: report the state without re-running the DB round trip on every poll. Written
#: once per process by :func:`run_startup_check`; ``None`` until then (a test
#: client that skips lifespan, or the worker before its first tick).
_last: dict | None = None


def last_result() -> dict | None:
    """The most recent :func:`check_all` result in this process, or ``None``."""
    return _last


def mismatches(result: dict | None = None) -> dict[str, str]:
    """``{name: reason}`` for every check currently in a BAD state.

    Empty on a healthy instance, which is what makes it drop straight into the
    ``degraded`` map ``/stats`` already publishes and the dashboard already
    knows how to render."""
    result = result if result is not None else _last
    if not result:
        return {}
    return {
        name: sub["reason"]
        for name, sub in result.items()
        if sub.get("state") in ("mismatch", "missing") and sub.get("reason")
    }


async def run_startup_check(settings=None) -> dict:
    """Lifespan entry point: run the checks, cache them, and LOG a mismatch.

    Owns its own session rather than taking one: it runs before any request
    exists. Logs at ERROR because that is the level the console Logs panel
    highlights and the level an operator's log shipper alerts on — the entire
    point of this feature is that the condition must not be quiet."""
    global _last
    from filearr.db import SessionLocal

    try:
        async with SessionLocal() as session:
            result = await check_all(session, settings)
    except Exception as exc:  # noqa: BLE001 — never block startup
        log.warning("keyguard: startup check could not run: %s", _reason(exc))
        result = {
            "secret_key": {"state": "unknown", "reason": _reason(exc)},
            "ca_root": {"state": "unknown", "reason": _reason(exc)},
        }
    _last = result
    for name, reason in mismatches(result).items():
        sub = result[name]
        log.error(
            "KEY FINGERPRINT MISMATCH (%s): %s [recorded %s%s, current %s]",
            name,
            reason,
            sub.get("recorded"),
            f" on {sub['recorded_at']}" if sub.get("recorded_at") else "",
            sub.get("current"),
        )
    return result


def fingerprints_for_manifest(settings=None) -> dict[str, str | None]:
    """The fingerprints a backup MANIFEST records (BK-T2/BK-T3).

    Environment-derived, so this needs no database and works from a bare script
    context. A restore can compare these against the target box's own
    environment BEFORE loading the dump — the check that would have caught the
    original incident at the moment it mattered. Values are fingerprints; the
    manifest is written to disk and must never carry a secret."""
    from filearr.config import get_settings

    settings = settings or get_settings()
    ca = getattr(settings, "ca_fingerprint", "") or ""
    return {
        SECRET_KEY_FP: fingerprint(settings.secret_key) if settings.secret_key else None,
        CA_ROOT_FP: _norm_ca(ca) if ca else None,
    }
