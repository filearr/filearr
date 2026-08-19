"""Channel dispatch drivers (Phase 8, P8-T2 + the P8-T4 crypto slice).

The pure, network-free cores — rule matching, windowing, SSRF classification,
HMAC signing — live in the sibling modules and are implemented + tested. This
module wires the async I/O: the ``webhook`` and ``email`` drivers (in-tree core,
brief §1.5) and the ``apprise`` fan-out driver (R2, P8-T3 — behind the optional
``filearr[apprise]`` extra), plus the channel-secret encryption helpers
(delegating to :mod:`filearr.alerts.crypto`).

Security posture (priority: security > integrity > reliability):

* **SSRF default-deny.** ``send_webhook`` resolves the target with a REAL
  ``socket.getaddrinfo`` resolver and vets EVERY A/AAAA record via
  :func:`filearr.alerts.ssrf.check_webhook_url` before connecting; a name host is
  then **pinned** to the validated IP for the actual socket, closing the
  DNS-rebinding TOCTOU. Redirects are never followed (a 3xx to a private IP is a
  classic SSRF-bypass vector). ``FILEARR_WEBHOOK_ALLOW_PRIVATE_CIDRS`` (R5) is the
  only widening, and it permits the ``private`` class only.
* **Signed payloads.** Every webhook body carries ``X-Filearr-Signature`` (HMAC
  via :func:`filearr.alerts.signing.sign_payload`) so the receiver can verify us.
* **Bounded I/O.** Per-request timeout + response-size cap.
* **Secrets never in the clear.** Channel secrets are AES-GCM ciphertext at rest;
  decrypted values are used in-process only and never logged.

``dispatch_locality`` (R6) is authoritative per channel; this driver layer does
not auto-detect reachability or dual-dispatch.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import socket
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

import httpx

from filearr.alerts import crypto, signing, ssrf, webhook_formats
from filearr.errors import sanitize_error

# Same logger name the dispatch pump uses (tasks/alerts.py) so channel-driver
# output lands in one place. Nothing logged here may contain a channel secret.
log = logging.getLogger("filearr.alerts")


@dataclass(frozen=True)
class RenderedAlert:
    """A rule match rendered once, then handed to each channel driver (§8.4).

    Built with the template-injection defenses from brief §5.3 (see
    :mod:`filearr.alerts.render`): file paths / filenames pass through
    ``errors.sanitize_error`` and are only ever template **variables**; the
    webhook ``payload`` is a plain dict serialized with ``json.dumps`` (never
    string-templated).
    """

    subject: str
    body_text: str
    payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DeliveryResult:
    """Normalized outcome of one channel send (Apprise's per-service result and
    the core drivers' results collapse into this so retry/backoff is uniform)."""

    ok: bool
    detail: str = ""
    status_code: int | None = None
    retryable: bool = False


class ChannelDeliveryError(Exception):
    """A delivery failure raised by a driver so the caller (P8-T6 dispatch
    worker) can retry or give up. ``retryable`` distinguishes a transient failure
    (connection refused / timeout / 5xx / SMTP 4xx) from a permanent rejection
    (SSRF block / refused redirect / 4xx / SMTP 5xx). ``detail`` is already
    sanitized + safe to store in ``alert_events.last_error``."""

    def __init__(
        self,
        detail: str,
        *,
        retryable: bool,
        status_code: int | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.retryable = retryable
        self.status_code = status_code


# --------------------------------------------------------------------------- #
# webhook (P8-T2)                                                              #
# --------------------------------------------------------------------------- #

def _system_resolver(host: str) -> list[str]:
    """Resolve ``host`` to all its A/AAAA records (blocking; run in a thread).

    Returns the de-duplicated list of IP strings. An empty list on failure lets
    :func:`ssrf.check_webhook_url` reject with ``no-dns-records`` rather than
    raising here."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except OSError:
        return []
    seen: list[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in seen:
            seen.append(ip)
    return seen


async def _threaded_resolver(host: str) -> list[str]:
    return await asyncio.to_thread(_system_resolver, host)


def _first_allowed_ip(
    resolved: tuple[tuple[str, ssrf.IpClass], ...], allow_private: bool, allowed_cidrs=()
) -> str | None:
    ok = ssrf._PRIVATE_OK if allow_private else ssrf._PUBLIC_OK
    for ip, cls in resolved:
        if cls in ok:
            return ip
        if cls is not ssrf.IpClass.UNSPECIFIED and ssrf._in_allowlist(ip, allowed_cidrs):
            return ip
    return None


def _allowed_cidrs():
    """The operator's explicit webhook target allowlist (parsed once per call;
    the list is tiny). Read lazily so the pure unit surface never needs settings."""
    try:
        from filearr.config import get_settings

        return ssrf.parse_allowed_cidrs(get_settings().webhook_allowed_cidrs)
    except Exception:  # noqa: BLE001 - settings unavailable => no allowlist
        return ()


def _pin_hostname(url: str, ip: str) -> str:
    """Rewrite ``url``'s hostname to ``ip`` (preserving scheme/port/path/query),
    bracketing IPv6. The original host travels in the Host header + TLS SNI so
    the socket connects to the validated IP and cannot be re-resolved to a
    rebound private address between validation and connect."""
    parts = urlsplit(url)
    host_ip = f"[{ip}]" if ":" in ip else ip
    netloc = host_ip if parts.port is None else f"{host_ip}:{parts.port}"
    if parts.username:  # preserve any userinfo (unusual for webhooks, but safe)
        creds = parts.username + (f":{parts.password}" if parts.password else "")
        netloc = f"{creds}@{netloc}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


async def send_webhook(
    url: str,
    payload: dict,
    *,
    secret: str | None = None,
    headers: dict[str, str] | None = None,
    timeout_s: float = 10.0,
    max_response_bytes: int = 65536,
    allow_private: bool = False,
    resolver: ssrf.Resolver | None = None,
    transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None,
    now: int | None = None,
) -> DeliveryResult:
    """POST ``payload`` to ``url`` with HMAC signing + SSRF guard (brief §7.1).

    Raises :class:`ChannelDeliveryError` on any failure (SSRF block, refused
    redirect, >=400, network/timeout); returns a :class:`DeliveryResult` on 2xx.
    ``resolver`` / ``transport`` are injectable for tests; production uses the
    threaded ``getaddrinfo`` resolver and a real ``httpx`` transport with
    ``follow_redirects=False`` (always — enforced here, not left to the caller).
    """
    sync_resolver = resolver or _system_resolver
    cidrs = _allowed_cidrs()
    verdict = ssrf.check_webhook_url(
        url, sync_resolver, allow_private=allow_private, allowed_cidrs=cidrs
    )
    if not verdict.allowed:
        # An SSRF-blocked target is a permanent, non-retryable rejection.
        raise ChannelDeliveryError(
            f"webhook target refused ({verdict.reason})",
            retryable=False,
        )

    body = json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str).encode(
        "utf-8"
    )
    ts = now if now is not None else int(time.time())
    req_headers: dict[str, str] = {
        "content-type": "application/json",
        **{k.lower(): v for k, v in (headers or {}).items()},
    }
    if secret:
        req_headers["x-filearr-signature"] = signing.sign_payload(secret, body, ts)

    # Pin a name host to its validated IP for the actual connection (rebinding
    # defense). Only in the real path — an injected transport short-circuits DNS.
    target_url = url
    extensions: dict | None = None
    parts = urlsplit(url)
    host = parts.hostname or ""
    is_ip_literal = True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        is_ip_literal = False
    if transport is None and not is_ip_literal:
        pinned = _first_allowed_ip(verdict.resolved, allow_private, cidrs)
        if pinned is not None:
            target_url = _pin_hostname(url, pinned)
            req_headers["host"] = parts.netloc
            extensions = {"sni_hostname": host}

    client_kwargs: dict = {
        "follow_redirects": False,  # a redirect is an SSRF-bypass vector
        "timeout": httpx.Timeout(timeout_s),
    }
    if transport is not None:
        client_kwargs["transport"] = transport
    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            request = client.build_request(
                "POST", target_url, content=body, headers=req_headers, extensions=extensions
            )
            resp = await client.send(request, stream=True)
            try:
                total = 0
                captured = bytearray()
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if len(captured) < max_response_bytes:
                        captured.extend(chunk[: max_response_bytes - len(captured)])
                    if total > max_response_bytes:
                        break
                status = resp.status_code
                detail_body = sanitize_error(bytes(captured).decode("utf-8", "replace"))
            finally:
                await resp.aclose()
    except httpx.TimeoutException as exc:
        raise ChannelDeliveryError(
            f"webhook timeout: {sanitize_error(exc)}", retryable=True
        ) from exc
    except httpx.TransportError as exc:
        raise ChannelDeliveryError(
            f"webhook transport error: {sanitize_error(exc)}", retryable=True
        ) from exc

    if 200 <= status < 300:
        return DeliveryResult(ok=True, status_code=status, detail=detail_body[:200])
    if 300 <= status < 400:
        raise ChannelDeliveryError(
            f"webhook redirect not followed (status {status})",
            retryable=False,
            status_code=status,
        )
    raise ChannelDeliveryError(
        f"webhook rejected (status {status}): {detail_body[:200]}",
        retryable=status >= 500,
        status_code=status,
    )



async def send_webhook_formatted(
    url: str,
    rendered: RenderedAlert,
    *,
    config: dict | None = None,
    secret: str | None = None,
    **send_kwargs,
) -> DeliveryResult:
    """FIX-16: POST a rendered alert to ``url`` in the channel's ``webhook_format``.

    ``generic`` (the back-compat default for any config lacking ``webhook_format``)
    sends ``rendered.payload`` byte-for-byte and keeps the HMAC signature; ``discord``
    / ``slack`` reshape the body (:mod:`filearr.alerts.webhook_formats`) so a foreign
    endpoint accepts it, and skip the signature (they never verify it). Every
    security control of :func:`send_webhook` (SSRF pinning, no-redirects, timeouts,
    size cap) is inherited unchanged via ``send_kwargs``."""
    fmt = webhook_formats.resolve_format(config)
    body = webhook_formats.format_body(
        fmt,
        subject=rendered.subject,
        body_text=rendered.body_text,
        payload=rendered.payload,
    )
    return await send_webhook(
        url,
        body,
        secret=webhook_formats.signing_secret(fmt, secret),
        **send_kwargs,
    )


# --------------------------------------------------------------------------- #
# email (P8-T2) — stdlib smtplib in a threadpool (no new dependency)          #
# --------------------------------------------------------------------------- #
# The P8 research recommended aiosmtplib; it is not a current dependency and the
# stdlib client covers STARTTLS/SSL/plain identically, so we use smtplib run off
# the event loop via asyncio.to_thread — zero new dependency surface (the
# security-first default). Swapping in aiosmtplib later keeps this signature.

_VALID_SECURITY = frozenset({"starttls", "ssl", "plain"})


def _send_email_sync(
    config: dict,
    rendered: RenderedAlert,
    timeout_s: float,
    attachment: tuple[str, bytes, str] | None = None,
) -> str:
    import smtplib
    from email.message import EmailMessage

    host = config.get("host")
    if not host:
        raise ChannelDeliveryError("email channel missing 'host'", retryable=False)
    port = int(config.get("port", 587))
    security = str(config.get("security", "starttls")).lower()
    if security not in _VALID_SECURITY:
        raise ChannelDeliveryError(
            f"email channel invalid security {security!r}", retryable=False
        )
    allow_insecure = bool(config.get("allow_insecure", False))
    if security == "plain" and not allow_insecure:
        # Refuse a plaintext downgrade unless explicitly opted out (brief §5.2).
        raise ChannelDeliveryError(
            "refusing plaintext SMTP (no STARTTLS/SSL); set allow_insecure to opt out",
            retryable=False,
        )
    sender = config.get("from_addr") or config.get("from")
    if not sender:
        raise ChannelDeliveryError("email channel missing 'from_addr'", retryable=False)
    recipients = config.get("to") or config.get("to_addrs") or []
    if isinstance(recipients, str):
        recipients = [recipients]
    recipients = [r for r in recipients if r]
    if not recipients:
        raise ChannelDeliveryError("email channel has no recipients", retryable=False)
    username = config.get("username")
    password = config.get("password")

    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = rendered.subject
    msg.set_content(rendered.body_text)  # text/plain; already sanitized upstream
    if attachment is not None:
        # P11-T9 scheduled-report delivery: attach the generated artifact when it
        # is under the configured size cap. filename is sanitized upstream (a
        # content-addressed / report-derived name, never a raw user path).
        att_name, att_bytes, att_mime = attachment
        maintype, _, subtype = att_mime.partition("/")
        msg.add_attachment(
            att_bytes,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=att_name,
        )

    try:
        if security == "ssl":
            server = smtplib.SMTP_SSL(host, port, timeout=timeout_s)
        else:
            server = smtplib.SMTP(host, port, timeout=timeout_s)
        with server:
            server.ehlo()
            if security == "starttls":
                server.starttls()
                server.ehlo()
            if username:
                server.login(username, password or "")
            server.send_message(msg, from_addr=sender, to_addrs=recipients)
    except smtplib.SMTPResponseException as exc:
        # 4xx SMTP = transient (retry); 5xx = permanent.
        code = getattr(exc, "smtp_code", 500) or 500
        raise ChannelDeliveryError(
            f"smtp error {code}: {sanitize_error(getattr(exc, 'smtp_error', exc))}",
            retryable=400 <= code < 500,
            status_code=code,
        ) from exc
    except (smtplib.SMTPException, OSError, TimeoutError) as exc:
        raise ChannelDeliveryError(
            f"smtp transport error: {sanitize_error(exc)}", retryable=True
        ) from exc
    return ", ".join(recipients)


async def send_email(
    config: dict,
    rendered: RenderedAlert,
    *,
    timeout_s: float = 30.0,
    attachment: tuple[str, bytes, str] | None = None,
) -> DeliveryResult:
    """Send ``rendered`` over SMTP (STARTTLS default; SSL/plain per config).

    Runs the blocking ``smtplib`` client off the event loop. Refuses a plaintext
    downgrade unless the channel explicitly opts out. Raises
    :class:`ChannelDeliveryError` (retryable-classified) on failure."""
    recipients = await asyncio.to_thread(
        _send_email_sync, config, rendered, timeout_s, attachment
    )
    return DeliveryResult(ok=True, detail=f"delivered to {recipients}")


# --------------------------------------------------------------------------- #
# apprise (P8-T3 — optional extra: the R2 fan-out channel)                    #
# --------------------------------------------------------------------------- #
# Apprise is the "everything else" channel: one URL string selects one of ~100
# notification services (Discord, Telegram, ntfy, Pushover, Matrix, Gotify, ...)
# so we do not have to grow a driver per service. It is an OPTIONAL extra
# (``pip install "filearr[apprise]"``) because it drags a large dependency tree
# (requests + requests-oauthlib + markdown + PyYAML + click) that the vast
# majority of deployments — webhook and/or SMTP only — never execute; see the
# rationale on the extra in backend/pyproject.toml.
#
# Security posture, inherited from the rest of this module: an apprise URL is a
# CREDENTIAL end to end (``discord://webhook_id/webhook_token``,
# ``tgram://bot_token/chat_id``), which is exactly why brief §7.2 makes the whole
# URL the encrypted sub-field (see :func:`encrypt_channel_secret`). Nothing in
# this section may put that string — or a fragment of it — into a log record, a
# ``DeliveryResult.detail`` or a ``ChannelDeliveryError.detail``, because
# ``alert_events.last_error`` is persisted and served to API clients.
#
# NOTE the deliberate asymmetry with :func:`send_webhook`: the SSRF guard is NOT
# applied here. It cannot be — apprise owns URL parsing and socket setup for every
# plugin, so there is no seam to vet a resolved address before the connect. The
# compensating control is that an apprise URL is admin-scope configuration
# carrying an embedded service credential, not an attacker-suppliable target.

_APPRISE_REDACTED = "<apprise-url redacted>"  # marker left where a URL was scrubbed


def _split_apprise_urls(raw: str) -> list[str]:
    """Split a channel's stored apprise config into individual target URLs.

    A multi-target channel is expressed as **one URL per line**, and each line is
    handed to its own ``Apprise.add()`` call (apprise's own native idiom — an
    ``Apprise`` object is a bag of servers that ``notify()`` fans out to). Newline
    is the only separator we accept: apprise URLs legitimately embed commas and
    spaces inside query parameters (``mailto://…?to=a@x.test,b@y.test``,
    ``?tags=a,b``), so a comma split would silently corrupt a valid credential
    into two unusable ones. Blank lines are dropped so trailing whitespace in the
    admin form is harmless."""
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _apprise_scheme(url: str) -> str:
    """The plugin scheme of ``url`` (``discord``, ``tgram``, …) — the only part of
    an apprise URL that is safe to log, since every credential lives after the
    ``://``. Returns ``"?"`` when the string has no scheme rather than falling
    back to the whole value (that fallback would leak the secret)."""
    scheme, sep, _ = url.partition("://")
    return scheme.lower() if sep and scheme else "?"


def _apprise_secret_fragments(urls: list[str]) -> list[str]:
    """Substrings that must never survive into a message, longest first.

    ``sanitize_error`` makes an untrusted string safe to *store* (control-char
    stripping + truncation); it does NOT redact, so an exception raised from
    inside a plugin can carry the configured URL verbatim. Full-string
    replacement alone is not enough either: plugins routinely raise with only a
    piece of the URL (a bot token, a room id), and apprise puts credentials in
    every positional slot — ``tgram://<token>/<chat_id>`` keeps them in the PATH,
    not in userinfo. So we scrub the whole URL *and* each of its components,
    ignoring fragments of 3 characters or fewer (a port or a one-letter path
    segment matches too much text to be worth redacting). Longest-first ordering
    means the full URL is replaced before any of its parts, which keeps a single
    marker in the common case instead of a shredded string."""
    fragments: set[str] = set()
    for url in urls:
        if not url:
            continue
        fragments.add(url)
        _, sep, remainder = url.partition("://")
        if not sep:
            continue
        for part in remainder.replace("?", "/").replace("&", "/").replace("@", "/").split("/"):
            if len(part) > 3:
                fragments.add(part)
    return sorted(fragments, key=len, reverse=True)


def _scrub_apprise(value: object, fragments: list[str]) -> str:
    """Redact ``fragments`` out of ``value``, then sanitize it for storage.

    Order is load-bearing and the reason this is not a one-liner: scrubbing runs
    on the RAW text first because ``sanitize_error`` truncates at
    ``MAX_ERROR_CHARS`` and a URL straddling that boundary would leave its
    surviving prefix — still credential-bearing — permanently in
    ``alert_events.last_error``. The second scrub pass catches the (pathological)
    case where control-character stripping splices a fragment back together."""
    text = str(value)
    for fragment in fragments:
        text = text.replace(fragment, _APPRISE_REDACTED)
    text = sanitize_error(text)
    for fragment in fragments:
        text = text.replace(fragment, _APPRISE_REDACTED)
    return text


def _notify_apprise_sync(apprise_mod, urls: list[str], rendered: RenderedAlert) -> bool:
    """Blocking apprise fan-out; runs in a worker thread (see the caller).

    Mirrors :func:`_send_email_sync`: the third-party client is synchronous, so
    the whole of it — object construction, URL parsing and every outbound HTTP
    request — happens off the event loop. A URL apprise refuses to parse is a
    PERMANENT configuration error (an unknown scheme or malformed credential will
    never start working on retry), so ``add()``'s boolean is checked here rather
    than being left to surface as an indistinguishable ``notify() -> False``."""
    client = apprise_mod.Apprise()
    for url in urls:
        if not client.add(url):
            # Only the scheme is quoted back; the URL itself is the credential.
            raise ChannelDeliveryError(
                f"apprise rejected a configured {_apprise_scheme(url)!r} URL "
                "(unknown service scheme or malformed URL)",
                retryable=False,
            )
    # notify_type is left at apprise's default ('info'): RenderedAlert carries no
    # severity field (render.py renders one body per rule match), so mapping to
    # warning/failure would be an invention rather than a translation.
    return bool(client.notify(title=rendered.subject, body=rendered.body_text))


async def send_via_apprise(
    apprise_url: str,
    rendered: RenderedAlert,
    *,
    timeout_s: float = 30.0,
) -> DeliveryResult:
    """P8-T3: dispatch via the optional ``apprise`` extra, normalized to DeliveryResult.

    ``apprise_url`` is the decrypted channel secret: one URL, or several separated
    by newlines (see :func:`_split_apprise_urls`). ``rendered`` is mapped onto
    apprise's ``title``/``body`` using the PLAINTEXT rendering — ``body_text``,
    never ``payload`` — because apprise re-encodes the body per service (Markdown
    for Discord, HTML for e-mail plugins) and a JSON blob would arrive as literal
    JSON in a chat window.

    Errors with an actionable message ("install filearr[apprise]") when the
    optional dependency is absent but an ``apprise``-type channel is configured:
    a missing optional dependency is a CONFIGURATION fault, so it is classified
    ``retryable=False`` and goes terminal on the first attempt instead of burning
    the group's ``alert_max_delivery_attempts`` budget on an outcome that cannot
    change until an operator acts.
    """
    urls = _split_apprise_urls(apprise_url or "")
    if not urls:
        # Checked BEFORE the import so a half-configured channel reports the field
        # it is actually missing rather than an unrelated dependency error.
        raise ChannelDeliveryError("apprise channel missing 'url'", retryable=False)

    # Imported lazily (never at module import) because the package is an optional
    # extra; a top-level import would make every worker process depend on it.
    # Deliberately imported HERE on the event loop rather than inside the worker
    # thread below: this is a one-time, purely local module load (no network), and
    # keeping it outside the wall-clock bound is what guarantees a missing extra is
    # reported as the permanent configuration error it is, instead of being
    # swallowed by a timeout and misclassified as retryable.
    try:
        import apprise
    except ImportError as exc:
        raise ChannelDeliveryError(
            "apprise channel configured but the 'apprise' package is not "
            'installed; install the optional extra: pip install "filearr[apprise]" '
            "(or use a Filearr image built with it)",
            retryable=False,
        ) from exc

    fragments = _apprise_secret_fragments(urls)
    log.debug(
        "apprise dispatch to %d target(s): %s",
        len(urls),
        ",".join(_apprise_scheme(u) for u in urls),  # schemes only — never the URLs
    )

    try:
        delivered = await asyncio.wait_for(
            asyncio.to_thread(_notify_apprise_sync, apprise, urls, rendered),
            timeout=timeout_s,
        )
    except ChannelDeliveryError:
        raise  # already classified inside the thread (unparsable URL)
    except TimeoutError as exc:
        # Same honest bound as ``extract_timeout_seconds`` (tasks/extract.py): a
        # Python thread cannot be killed, so this frees the CALLER, not the
        # thread. The abandoned thread may keep waiting on whatever socket apprise
        # opened until it returns on its own (apprise's plugins set their own
        # request timeouts) — what we guarantee is that the dispatch pump keeps
        # moving instead of wedging on one channel. Retryable: an overrun is a
        # transient-looking condition (slow or unreachable service).
        raise ChannelDeliveryError(
            f"apprise delivery exceeded {timeout_s}s (thread abandoned; "
            "tune FILEARR_ALERT_APPRISE_TIMEOUT_S)",
            retryable=True,
        ) from exc
    except Exception as exc:
        # An exception escaping apprise is a plugin/transport failure; treat it as
        # transient (the webhook driver classifies transport errors the same way).
        # The text is scrubbed because a plugin may quote the URL back at us.
        raise ChannelDeliveryError(
            f"apprise raised: {_scrub_apprise(exc, fragments)}", retryable=True
        ) from exc

    if not delivered:
        # apprise's notify() collapses every per-service failure into one bool and
        # swallows the reason (it logs the detail through its own 'apprise' logger
        # and returns False), so there is genuinely nothing more specific to
        # report here. Retryable: the usual causes — service 5xx, rate limit,
        # transient DNS — clear on their own.
        raise ChannelDeliveryError(
            f"apprise reported failed delivery for {len(urls)} target(s); "
            "apprise swallows the per-service reason — see the 'apprise' logger "
            "output in the worker log for the detail",
            retryable=True,
        )
    return DeliveryResult(ok=True, detail=f"apprise delivered to {len(urls)} target(s)")


# --------------------------------------------------------------------------- #
# channel-secret encryption (P8-T4 slice — delegates to alerts.crypto)        #
# --------------------------------------------------------------------------- #

def encrypt_channel_secret(plaintext: str, key: bytes) -> str:
    """Envelope-encrypt a channel secret with AES-GCM (see :mod:`.crypto`).

    For an ``apprise``-type channel the encryption boundary is the **whole** URL
    string (it embeds tokens inline), not a sub-field (brief §7.2)."""
    return crypto.encrypt_secret(plaintext, key)


def decrypt_channel_secret(ciphertext: str, key: bytes) -> str:
    """Inverse of :func:`encrypt_channel_secret`; in-process at dispatch only.

    Decrypted values are never logged and never returned to the API client.

    BK-T1: a failure here is, in practice, almost never corruption — it is a
    WRONG FILEARR_SECRET_KEY, and this is the exact point at which the silent
    restore failure finally becomes visible ("the alert that quietly stopped
    sending"). So the raised error names that cause and points at the
    fingerprint comparison, instead of leaving an operator with GCM's
    (accurate but unactionable) "wrong key or tampering"."""
    try:
        return crypto.decrypt_secret(ciphertext, key)
    except crypto.SecretDecryptError as exc:
        from filearr import keyguard

        fp = keyguard.last_result() or {}
        state = (fp.get("secret_key") or {}).get("state")
        hint = (
            " The key-fingerprint guard reports FILEARR_SECRET_KEY does NOT "
            "match what this database was encrypted under — see the About page."
            if state in ("mismatch", "missing")
            else " Most often this means FILEARR_SECRET_KEY differs from the "
            "one this secret was encrypted under (e.g. a restore onto a box "
            "with a freshly generated key)."
        )
        raise crypto.SecretDecryptError(str(exc) + hint) from exc
