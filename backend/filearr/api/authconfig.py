"""GUI auth-provider configuration API (2026-08-20, admin).

Read/write the LDAP login, AD directory-sync and OIDC configs the console
manages (see :mod:`filearr.authconfig`), plus **test** actions that validate a
proposed config against the FORM values (not the stored ones) before an admin
commits it: an LDAP service-bind + sample enumeration, and an OIDC discovery
fetch. Secrets are never returned; the client sends
``filearr.authconfig.SECRET_UNCHANGED`` to keep an existing one.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from filearr import audit, authconfig
from filearr.db import get_session
from filearr.security import require_scope

router = APIRouter()

_VALID = {"ldap", "directory", "oidc"}


def _check_provider(provider: str) -> None:
    if provider not in _VALID:
        raise HTTPException(404, f"unknown provider {provider!r}")


@router.get("/{provider}", dependencies=[Depends(require_scope("admin"))])
async def get_provider_config(
    provider: str, session: AsyncSession = Depends(get_session)
) -> dict:
    """The effective config for one provider with secrets redacted to ``has_*``
    flags and a per-field ``_source`` map (gui | env). Absent GUI fields fall
    through to the env value so the form is pre-populated with what is live."""
    _check_provider(provider)
    return await authconfig.get_config(session, provider)


@router.put("/{provider}", dependencies=[Depends(require_scope("admin"))])
async def set_provider_config(
    provider: str,
    request: Request,
    body: dict[str, Any] = Body(...),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Persist a provider config (admin). Only allow-listed fields are accepted;
    secrets are encrypted, ``SECRET_UNCHANGED`` keeps the stored one, ``""``
    clears it. Returns the redacted stored config."""
    _check_provider(provider)
    principal = getattr(request.state, "actor", None)
    updated_by = None
    if isinstance(principal, str) and principal.startswith("principal:"):
        import uuid as _uuid

        try:
            updated_by = _uuid.UUID(principal.split(":", 1)[1])
        except ValueError:
            updated_by = None
    try:
        await authconfig.set_config(session, provider, body, updated_by=updated_by)
    except authconfig.AuthConfigError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - e.g. SecretKeyMissing
        raise HTTPException(422, f"could not save config: {exc}") from exc
    await session.commit()
    # Audit the field NAMES only — never the values (a bind DN / issuer is
    # config, but the secret values must not reach the audit log).
    await audit.emit(
        audit.AUTH_CONFIG_CHANGED,
        request=request,
        details={"provider": provider, "fields": sorted(body.keys())},
    )
    return await authconfig.get_config(session, provider)


# --------------------------------------------------------------------------- #
# Fetch the server's certificate chain (so the admin doesn't hand-export it)    #
# --------------------------------------------------------------------------- #
def _fetch_chain(host: str, port: int, timeout: float) -> list[bytes]:
    """The DER certificate chain a TLS server presents, WITHOUT validation
    (trust-on-first-use — the caller must verify the fingerprint out of band).
    Blocking; run in a threadpool."""
    import socket
    import ssl

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as ssock:
            return list(ssock.get_unverified_chain() or [])


def _describe_cert(der: bytes) -> dict:
    import hashlib

    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    cert = x509.load_der_x509_certificate(der)
    try:
        is_ca = cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    except x509.ExtensionNotFound:
        is_ca = False
    pem = cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
    return {
        "subject": cert.subject.rfc4514_string(),
        "issuer": cert.issuer.rfc4514_string(),
        "sha256": hashlib.sha256(der).hexdigest(),
        "not_after": cert.not_valid_after_utc.isoformat(),
        "is_ca": bool(is_ca),
        "is_self_signed": cert.subject == cert.issuer,
        "pem": pem,
    }


# AD DCs frequently present ONLY their leaf certificate on LDAPS (schannel
# omits intermediates), so the handshake alone often can't produce a CA anchor.
# The leaf's Authority Information Access extension points at the issuing CA's
# certificate — follow it, bounded. SSRF surface: this admin-only TOFU endpoint
# already dials an arbitrary admin-supplied host; AIA URLs additionally come
# from the (unverified) certificate, so fetches are capped in scheme
# (http/https), size, redirects and depth, and the response is only ever parsed
# as X.509/PKCS#7 — never echoed back raw.
_AIA_MAX_DEPTH = 4
_AIA_MAX_BYTES = 1024 * 1024


def _http_get(url: str, timeout: float) -> bytes:
    """Bounded GET for an AIA CA-Issuers payload (test seam)."""
    import httpx

    with httpx.Client(timeout=timeout, follow_redirects=True, max_redirects=3) as client:
        resp = client.get(url)
        resp.raise_for_status()
        if len(resp.content) > _AIA_MAX_BYTES:
            raise ValueError(f"AIA payload exceeds {_AIA_MAX_BYTES} bytes")
        return resp.content


def _aia_ca_issuer_urls(der: bytes) -> list[str]:
    from cryptography import x509
    from cryptography.x509.oid import AuthorityInformationAccessOID, ExtensionOID

    cert = x509.load_der_x509_certificate(der)
    try:
        aia = cert.extensions.get_extension_for_oid(
            ExtensionOID.AUTHORITY_INFORMATION_ACCESS
        ).value
    except x509.ExtensionNotFound:
        return []
    urls: list[str] = []
    for ad in aia:
        if ad.access_method == AuthorityInformationAccessOID.CA_ISSUERS and isinstance(
            ad.access_location, x509.UniformResourceIdentifier
        ):
            url = ad.access_location.value or ""
            if url.lower().startswith(("http://", "https://")):
                urls.append(url)
    return urls


def _certs_from_payload(data: bytes) -> list[bytes]:
    """DER certificates from an AIA payload — AD CS serves bare X.509 (DER or
    PEM) or a PKCS#7 bundle depending on how the CA was published."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import pkcs7

    der = serialization.Encoding.DER
    try:
        return [x509.load_der_x509_certificate(data).public_bytes(der)]
    except Exception:  # noqa: BLE001
        pass
    try:
        return [c.public_bytes(der) for c in x509.load_pem_x509_certificates(data)]
    except Exception:  # noqa: BLE001
        pass
    for loader in (pkcs7.load_der_pkcs7_certificates, pkcs7.load_pem_pkcs7_certificates):
        try:
            return [c.public_bytes(der) for c in loader(data)]
        except Exception:  # noqa: BLE001
            continue
    return []


def _complete_chain_via_aia(chain_der: list[bytes], timeout: float) -> list[bytes]:
    """Missing CA certificates for ``chain_der``, fetched by walking the top
    certificate's AIA ``CA Issuers`` pointers until the chain is rooted (top
    cert self-signed) or nothing further is reachable. Best-effort: any fetch
    or parse failure just stops the walk. Blocking; run in a threadpool."""
    import hashlib

    from cryptography import x509

    fetched: list[bytes] = []
    seen = {hashlib.sha256(d).hexdigest() for d in chain_der}
    top = chain_der[-1]
    for _ in range(_AIA_MAX_DEPTH):
        top_cert = x509.load_der_x509_certificate(top)
        if top_cert.subject == top_cert.issuer:
            break  # rooted
        nxt: bytes | None = None
        for url in _aia_ca_issuer_urls(top):
            try:
                candidates = _certs_from_payload(_http_get(url, timeout))
            except Exception:  # noqa: BLE001
                continue
            for cand in candidates:
                cand_cert = x509.load_der_x509_certificate(cand)
                if cand_cert.subject == top_cert.issuer:
                    nxt = cand
                    break
            if nxt is not None:
                break
        if nxt is None:
            break
        digest = hashlib.sha256(nxt).hexdigest()
        if digest in seen:
            break  # loop guard (cross-signed rings)
        seen.add(digest)
        fetched.append(nxt)
        top = nxt
    return fetched


@router.post("/ldap/fetch-cert", dependencies=[Depends(require_scope("admin"))])
async def fetch_ldap_cert(
    body: dict[str, Any] = Body(default_factory=dict),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Pull the certificate chain the LDAPS server presents so an admin can trust
    it from the console instead of hand-exporting a CA file.

    Connects to the ``server`` (from the form, else the saved/env config) WITHOUT
    validating — this is trust-on-first-use, so the response includes every
    cert's subject/issuer/SHA-256 fingerprint for the admin to verify against a
    known-good value before trusting. When the server presents an incomplete
    chain (AD DCs commonly send only their leaf), the missing CA certificates
    are fetched by following the AIA ``CA Issuers`` pointers (marked
    ``via_aia`` in the response). ``suggested_ca_pem`` is the completed chain
    MINUS the leaf (the issuing-CA anchor — surviving leaf renewal); only if no
    CA cert could be obtained at all is the leaf suggested, with a warning.
    Nothing is saved — the admin reviews, then Saves the PEM into the config."""
    from urllib.parse import urlsplit

    from starlette.concurrency import run_in_threadpool

    eff = await authconfig.effective_settings_with_overlay(session, "ldap", body)
    server = (body.get("ldap_server") or eff.ldap_server or "").strip()
    if not server:
        raise HTTPException(422, "no LDAP server URL to fetch from")
    parts = urlsplit(server if "://" in server else f"ldaps://{server}")
    host = parts.hostname or ""
    port = parts.port or (636 if (parts.scheme or "ldaps") == "ldaps" else 389)
    if not host:
        raise HTTPException(422, f"could not parse a host from {server!r}")
    timeout = float(getattr(eff, "ldap_timeout", 10) or 10)
    try:
        chain_der = await run_in_threadpool(_fetch_chain, host, port, timeout)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"could not connect to {host}:{port}: {exc}"}
    if not chain_der:
        return {"ok": False, "error": "server presented no certificate"}
    aia_der = await run_in_threadpool(_complete_chain_via_aia, chain_der, timeout)
    chain = [{**_describe_cert(d), "via_aia": False} for d in chain_der] + [
        {**_describe_cert(d), "via_aia": True} for d in aia_der
    ]
    # Prefer the issuing CA(s) — everything above the leaf — as the trust anchor.
    if len(chain) > 1:
        suggested = "".join(c["pem"] for c in chain[1:])
        note = "trusting the issuing CA chain (survives leaf-certificate renewal)"
        if aia_der:
            note += (
                "; the server sent an incomplete chain — "
                f"{len(aia_der)} CA certificate(s) were retrieved via the "
                "certificate's AIA pointer"
            )
    else:
        suggested = chain[0]["pem"]
        note = ("server sent only its leaf certificate and it carries no reachable "
                "AIA pointer; trusting the leaf directly will break when that "
                "certificate is renewed — prefer importing the CA")
    return {
        "ok": True,
        "host": host,
        "port": port,
        "chain": [{k: v for k, v in c.items() if k != "pem"} for c in chain],
        "suggested_ca_pem": suggested,
        "note": note,
    }


# --------------------------------------------------------------------------- #
# Test actions (validate the FORM values without saving)                       #
# --------------------------------------------------------------------------- #
@router.post("/ldap/test", dependencies=[Depends(require_scope("admin"))])
async def test_ldap(
    body: dict[str, Any] = Body(default_factory=dict),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Service-bind + a bounded sample enumeration against the proposed LDAP
    config (form values overlaid on the stored+env config, secrets from the
    store when omitted). Never persists. Returns ``{ok, detail, users, groups}``
    or ``{ok: false, error}``."""
    from starlette.concurrency import run_in_threadpool

    from filearr import ldap_directory
    from filearr.ldap_auth import LdapConfig, LDAPError, connect

    eff = await authconfig.effective_settings_with_overlay(session, "ldap", body)
    try:
        cfg = LdapConfig.from_settings(eff)
    except LDAPError as exc:
        return {"ok": False, "error": f"{exc.reason}: {exc.detail}"}
    if not cfg.bind_dn:
        return {"ok": False, "error": "a service bind (bind_dn/password) is required to test"}
    try:
        dcfg = ldap_directory.DirectoryConfig.from_settings(eff)
        entries = await run_in_threadpool(
            ldap_directory.enumerate_directory, cfg, dcfg, connector=connect
        )
    except LDAPError as exc:
        return {"ok": False, "error": f"{exc.reason}: {exc.detail}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"enumeration failed: {exc}"}
    users = sum(1 for e in entries if e.kind == "user")
    groups = sum(1 for e in entries if e.kind == "group")
    return {
        "ok": True,
        "detail": f"bound as {cfg.bind_dn}; enumerated {len(entries)} objects",
        "users": users,
        "groups": groups,
    }


@router.post("/directory/test", dependencies=[Depends(require_scope("admin"))])
async def test_directory(
    body: dict[str, Any] = Body(default_factory=dict),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Enumerate EVERY configured directory endpoint (cross-forest included)
    against the proposed config, reporting per-endpoint counts + errors without
    persisting or reconciling."""
    from starlette.concurrency import run_in_threadpool

    from filearr import ldap_directory
    from filearr.ldap_auth import LDAPError, connect

    eff = await authconfig.effective_settings_with_overlay(session, "directory", body)
    try:
        endpoints = ldap_directory.endpoints_from_settings(eff)
    except LDAPError as exc:
        return {"ok": False, "error": f"{exc.reason}: {exc.detail}"}
    results = []
    ok = True
    for ep in endpoints:
        try:
            entries = await run_in_threadpool(
                ldap_directory.enumerate_directory, ep.ldap, ep.dcfg, connector=connect
            )
            results.append({
                "label": ep.label,
                "ok": True,
                "users": sum(1 for e in entries if e.kind == "user"),
                "groups": sum(1 for e in entries if e.kind == "group"),
            })
        except (LDAPError, Exception) as exc:  # noqa: BLE001
            ok = False
            reason = getattr(exc, "reason", None)
            results.append({"label": ep.label, "ok": False,
                            "error": f"{reason or type(exc).__name__}: {exc}"})
    return {"ok": ok, "endpoints": results}


@router.post("/oidc/test", dependencies=[Depends(require_scope("admin"))])
async def test_oidc(
    body: dict[str, Any] = Body(default_factory=dict),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Fetch + validate the OIDC discovery document and JWKS for the proposed
    issuer/client, without persisting. Returns ``{ok, issuer, endpoints}`` or an
    error (a bad issuer URL, an unreachable discovery doc, or an issuer
    mismatch)."""
    from filearr import oidc as oidc_mod

    eff = await authconfig.effective_settings_with_overlay(session, "oidc", body)
    if not (eff.oidc_issuer and eff.oidc_client_id):
        return {"ok": False, "error": "issuer and client_id are required to test"}
    try:
        cfg = oidc_mod.OidcConfig.from_settings(eff)
        meta = await oidc_mod.fetch_metadata(cfg)
        # A JWKS fetch proves the signing keys are reachable (id-token validation).
        await oidc_mod.fetch_jwks(cfg, meta["jwks_uri"])
    except oidc_mod.OIDCError as exc:
        return {"ok": False, "error": f"{exc.reason}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"discovery failed: {exc}"}
    return {
        "ok": True,
        "issuer": meta.get("issuer"),
        "authorization_endpoint": meta.get("authorization_endpoint"),
        "token_endpoint": meta.get("token_endpoint"),
        "jwks_uri": meta.get("jwks_uri"),
    }
