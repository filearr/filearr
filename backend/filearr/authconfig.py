"""GUI-editable auth-provider configuration (2026-08-20).

LDAP/AD login, the AD directory sync, and OIDC SSO were env-only
(``FILEARR_LDAP_*`` / ``FILEARR_OIDC_*``). This module lets an admin configure
them from the console instead, WITHOUT losing the env path: a value saved here
**overrides** the matching env default per field (the env stays the
bootstrap/fallback so the first admin can still sign in before anything is
configured), and :func:`effective_settings` produces the merged
:class:`~filearr.config.Settings` every auth reader uses.

Storage: three JSON blobs in the ``app_settings`` KV table
(``ldap_config`` / ``ldap_directory_config`` / ``oidc_config``), keyed by the
exact ``Settings`` field name so the overlay is a plain ``model_copy(update=…)``.
A strict per-provider field allow-list is the injection guard — the API can only
ever set fields named here, never arbitrary Settings.

Secrets (``ldap_bind_password``, ``oidc_client_secret``, and each cross-forest
endpoint's ``bind_password``) are AES-GCM encrypted at rest under
``FILEARR_SECRET_KEY`` (the same key/scheme as alert-channel secrets) and are
NEVER returned by the read API — the client sees a ``has_*`` boolean and sends
the :data:`SECRET_UNCHANGED` sentinel to keep an existing secret on save.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from filearr import app_settings
from filearr.alerts import crypto
from filearr.config import Settings, get_settings

logger = logging.getLogger("filearr.authconfig")

# A sentinel a client sends to mean "keep the stored secret" (vs. "" = clear it).
SECRET_UNCHANGED = "__unchanged__"

# --- Per-provider field allow-lists (the injection guard) ------------------- #
# Non-secret LDAP-login fields.
LDAP_FIELDS: frozenset[str] = frozenset(
    {
        "ldap_enabled", "ldap_server", "ldap_start_tls", "ldap_allow_plaintext",
        "ldap_tls_verify", "ldap_tls_ca_cert_file", "ldap_tls_ca_cert_pem",
        "ldap_timeout", "ldap_bind_dn",
        "ldap_user_dn_template", "ldap_user_base", "ldap_user_filter",
        "ldap_attr_username", "ldap_attr_email", "ldap_attr_uid", "ldap_use_memberof",
        "ldap_username_format", "ldap_attr_upn", "ldap_attr_display_name",
        "ldap_attr_memberof", "ldap_group_base", "ldap_group_filter", "ldap_role_map",
        "ldap_default_role", "ldap_auto_provision", "ldap_group_sync",
    }
)
LDAP_SECRETS: frozenset[str] = frozenset({"ldap_bind_password"})

DIRECTORY_FIELDS: frozenset[str] = frozenset(
    {
        "ldap_directory_sync_enabled", "ldap_directory_user_base",
        "ldap_directory_group_base", "ldap_directory_user_filter",
        "ldap_directory_group_filter", "ldap_attr_object_sid", "ldap_attr_object_guid",
        "ldap_attr_display_name", "ldap_attr_sam", "ldap_attr_upn",
        "ldap_attr_member_of_dir", "ldap_directory_domain", "ldap_directory_page_size",
        "ldap_directory_max_objects", "ldap_directories",
    }
)
# ldap_directories is a LIST of endpoint dicts; each may carry a bind_password.
DIRECTORY_SECRETS: frozenset[str] = frozenset()

OIDC_FIELDS: frozenset[str] = frozenset(
    {
        "oidc_enabled", "oidc_issuer", "oidc_client_id", "oidc_scopes",
        "oidc_redirect_uri", "oidc_role_claim", "oidc_role_map", "oidc_default_role",
        "oidc_auto_provision", "oidc_username_claim", "oidc_link_by_email",
        "oidc_group_claim", "oidc_login_state_ttl_minutes", "oidc_http_timeout_s",
    }
)
OIDC_SECRETS: frozenset[str] = frozenset({"oidc_client_secret"})

_PROVIDERS = {
    "ldap": (app_settings.KEY_LDAP_CONFIG, LDAP_FIELDS, LDAP_SECRETS),
    "directory": (app_settings.KEY_LDAP_DIRECTORY_CONFIG, DIRECTORY_FIELDS, DIRECTORY_SECRETS),
    "oidc": (app_settings.KEY_OIDC_CONFIG, OIDC_FIELDS, OIDC_SECRETS),
}


class AuthConfigError(ValueError):
    """A rejected auth-config write (unknown field, missing secret key, ...)."""


def validate_pem_chain(pem: str) -> int:
    """Parse a pasted/fetched CA PEM bundle; return the certificate count.

    Raises :class:`AuthConfigError` if it holds no valid X.509 certificate, so a
    typo/wrong paste is rejected at save time rather than breaking every login.
    ldap3 loads this via ``load_verify_locations(cadata=…)``; we validate with
    ``cryptography`` (already a dependency) which is stricter than ssl."""
    from cryptography import x509

    text = (pem or "").strip()
    if "BEGIN CERTIFICATE" not in text:
        raise AuthConfigError("not a PEM certificate (expected -----BEGIN CERTIFICATE-----)")
    blocks = [b for b in text.split("-----END CERTIFICATE-----") if "BEGIN CERTIFICATE" in b]
    count = 0
    for b in blocks:
        pem_one = (b + "-----END CERTIFICATE-----\n").encode("utf-8")
        try:
            x509.load_pem_x509_certificate(pem_one)
            count += 1
        except Exception as exc:  # noqa: BLE001
            raise AuthConfigError(f"invalid certificate in PEM bundle: {exc}") from exc
    if count == 0:
        raise AuthConfigError("PEM contained no parseable certificate")
    return count


# --------------------------------------------------------------------------- #
# Secret helpers                                                               #
# --------------------------------------------------------------------------- #
def _enc(plaintext: str) -> str:
    return crypto.encrypt_secret(plaintext, crypto.require_content_key())


def _dec(token: str) -> str | None:
    key = crypto.get_content_key()
    if key is None:
        return None
    try:
        return crypto.decrypt_secret(token, key)
    except crypto.SecretDecryptError:
        # A wrong FILEARR_SECRET_KEY (e.g. after a restore) — refuse to serve a
        # broken bind password rather than crash the whole auth path.
        logger.error("authconfig: stored secret failed to decrypt (wrong FILEARR_SECRET_KEY?)")
        return None


def _endpoint_secret_fields() -> frozenset[str]:
    return frozenset({"bind_password"})


# --------------------------------------------------------------------------- #
# Read (redacted) — for the admin API                                          #
# --------------------------------------------------------------------------- #
async def get_config(session: AsyncSession, provider: str) -> dict[str, Any]:
    """The stored config for ``provider`` with secrets REDACTED to ``has_*``
    booleans, plus a per-field ``_source`` map ('gui' | 'env') so the UI can show
    where each effective value comes from. Absent fields fall through to env."""
    key, fields, secrets = _providers(provider)
    stored: dict[str, Any] = dict(await app_settings.get_value(session, key) or {})
    env = get_settings()
    out: dict[str, Any] = {}
    source: dict[str, str] = {}
    for f in sorted(fields):
        if f in stored:
            out[f] = _redact_field(f, stored[f])
            source[f] = "gui"
        else:
            out[f] = getattr(env, f, None)
            source[f] = "env"
    for f in sorted(secrets):
        has = bool(stored.get(f)) or bool(getattr(env, f, None))
        out[f"has_{f}"] = has
        source[f] = "gui" if f in stored else ("env" if getattr(env, f, None) else "unset")
    out["_source"] = source
    return out


def _redact_field(field: str, value: Any) -> Any:
    """Redact per-endpoint secrets inside the ldap_directories list; every other
    non-secret field is returned as-is."""
    if field == "ldap_directories" and isinstance(value, list):
        red = []
        for ep in value:
            if isinstance(ep, dict):
                e = {k: v for k, v in ep.items() if k not in _endpoint_secret_fields()}
                e["has_bind_password"] = bool(ep.get("bind_password"))
                red.append(e)
        return red
    return value


# --------------------------------------------------------------------------- #
# Write                                                                         #
# --------------------------------------------------------------------------- #
async def set_config(
    session: AsyncSession, provider: str, incoming: dict[str, Any], *, updated_by=None
) -> None:
    """Validate + persist a provider config. Only allow-listed fields are kept;
    an unknown field is an error (not silently dropped) so a typo surfaces.
    Secrets are encrypted; the :data:`SECRET_UNCHANGED` sentinel keeps the stored
    value, and ``""`` clears it."""
    key, fields, secrets = _providers(provider)
    stored: dict[str, Any] = dict(await app_settings.get_value(session, key) or {})
    allowed = fields | secrets
    unknown = set(incoming) - allowed
    if unknown:
        raise AuthConfigError(f"unknown {provider} fields: {', '.join(sorted(unknown))}")

    new_blob = dict(stored)
    for f, v in incoming.items():
        if f in secrets:
            _apply_secret(new_blob, f, v)
        elif f == "ldap_tls_ca_cert_pem":
            if v:
                validate_pem_chain(str(v))
                new_blob[f] = str(v)
            else:
                new_blob.pop(f, None)
        elif f == "ldap_directories":
            new_blob[f] = _merge_endpoints(stored.get(f) or [], v)
        else:
            if v is None:
                new_blob.pop(f, None)
            else:
                new_blob[f] = v
    await app_settings.set_value(session, key, new_blob, updated_by=updated_by)


def _apply_secret(blob: dict, field: str, value: Any) -> None:
    if value == SECRET_UNCHANGED:
        return  # keep whatever is stored
    if not value:
        blob.pop(field, None)  # clear
        return
    blob[field] = _enc(str(value))


def _merge_endpoints(stored: list, incoming: Any) -> list:
    """Merge the cross-forest endpoint list, preserving each endpoint's stored
    bind_password when the client sends the SECRET_UNCHANGED sentinel (matched by
    the endpoint's ``server``)."""
    if not isinstance(incoming, list):
        raise AuthConfigError("ldap_directories must be a list")
    by_server = {e.get("server"): e for e in stored if isinstance(e, dict)}
    out = []
    for ep in incoming:
        if not isinstance(ep, dict) or not ep.get("server"):
            raise AuthConfigError("each ldap_directories entry needs a 'server'")
        e = dict(ep)
        pw = e.get("bind_password")
        if pw == SECRET_UNCHANGED:
            prev = by_server.get(e["server"], {})
            if prev.get("bind_password"):
                e["bind_password"] = prev["bind_password"]
            else:
                e.pop("bind_password", None)
        elif pw:
            e["bind_password"] = _enc(str(pw))
        else:
            e.pop("bind_password", None)
        out.append(e)
    return out


# --------------------------------------------------------------------------- #
# Effective settings (the overlay every auth reader uses)                      #
# --------------------------------------------------------------------------- #
async def effective_settings(session: AsyncSession) -> Settings:
    """Env :class:`Settings` with the GUI auth blobs overlaid (GUI wins per
    field), secrets decrypted, ready for ``LdapConfig.from_settings`` /
    ``OIDCConfig`` / the directory sync. Falls back to pure env on any load error
    so a transient DB blip never locks everyone out."""
    env = get_settings()
    try:
        overlay: dict[str, Any] = {}
        for _provider, (key, fields, secrets) in _PROVIDERS.items():
            blob = await app_settings.get_value(session, key)
            if not isinstance(blob, dict):
                continue
            for f, v in blob.items():
                if f in secrets:
                    dec = _dec(str(v)) if v else None
                    if dec is not None:
                        overlay[f] = dec
                elif f == "ldap_directories" and isinstance(v, list):
                    overlay[f] = _decrypt_endpoints(v)
                elif f in fields:
                    overlay[f] = v
        return env.model_copy(update=overlay) if overlay else env
    except Exception:  # noqa: BLE001 - never let a config read break auth
        logger.warning("authconfig.effective_settings: falling back to env", exc_info=True)
        return env


async def effective_settings_with_overlay(
    session: AsyncSession, provider: str, form: dict[str, Any]
) -> Settings:
    """Effective settings PLUS an unsaved ``form`` overlay for one provider — the
    exact config a save would produce, for the pre-save Test actions. Never
    persists. Secret handling mirrors :func:`set_config`: ``SECRET_UNCHANGED``
    keeps the currently-effective (stored/env) secret, ``""`` clears it, a value
    is used verbatim (plaintext — the test uses it directly, nothing stored)."""
    _key, fields, secrets = _providers(provider)
    base = await effective_settings(session)  # env + stored GUI, decrypted
    overlay: dict[str, Any] = {}
    for f, v in (form or {}).items():
        if f in secrets:
            if v == SECRET_UNCHANGED:
                continue  # base already carries the effective secret
            overlay[f] = str(v) if v else None
        elif f == "ldap_directories":
            overlay[f] = _overlay_endpoints(getattr(base, "ldap_directories", None) or [], v)
        elif f in fields:
            overlay[f] = v
    return base.model_copy(update=overlay) if overlay else base


def _overlay_endpoints(base_endpoints: list, incoming: Any) -> list:
    """Endpoint list for a test: decrypted base endpoints (by server) supply the
    bind_password when the form sends SECRET_UNCHANGED."""
    if not isinstance(incoming, list):
        raise AuthConfigError("ldap_directories must be a list")
    by_server = {e.get("server"): e for e in base_endpoints if isinstance(e, dict)}
    out = []
    for ep in incoming:
        if not isinstance(ep, dict) or not ep.get("server"):
            raise AuthConfigError("each ldap_directories entry needs a 'server'")
        e = dict(ep)
        pw = e.get("bind_password")
        if pw == SECRET_UNCHANGED:
            prev = by_server.get(e["server"], {})
            if prev.get("bind_password"):
                e["bind_password"] = prev["bind_password"]
            else:
                e.pop("bind_password", None)
        elif not pw:
            e.pop("bind_password", None)
        out.append(e)
    return out


def _decrypt_endpoints(endpoints: list) -> list:
    out = []
    for ep in endpoints:
        if not isinstance(ep, dict):
            continue
        e = dict(ep)
        if e.get("bind_password"):
            dec = _dec(str(e["bind_password"]))
            if dec is not None:
                e["bind_password"] = dec
            else:
                e.pop("bind_password", None)
        out.append(e)
    return out


def _providers(provider: str):
    try:
        return _PROVIDERS[provider]
    except KeyError as exc:
        raise AuthConfigError(f"unknown provider {provider!r}") from exc
