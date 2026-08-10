"""P8-T3: the apprise channel driver (optional ``filearr[apprise]`` extra).

Every test here runs WITHOUT apprise installed — the extra is optional by design
(see the rationale on ``[project.optional-dependencies]`` in pyproject.toml), so
the suite must never depend on it being present. A fake module is injected into
``sys.modules`` (which is what ``send_via_apprise``'s lazy ``import apprise``
consults), and the missing-dependency case is produced by pinning ``None`` there
so the import genuinely raises — the same mechanism a deployment without the
extra hits.

The security assertion that matters most is the last group: an apprise URL is a
credential end to end (brief §7.2), so it must never reach a log record, a
``DeliveryResult`` or a ``ChannelDeliveryError.detail`` — the latter is persisted
into ``alert_events.last_error`` and served to API clients.
"""

from __future__ import annotations

import logging
import sys
import types

import pytest

from filearr.alerts.dispatch import (
    ChannelDeliveryError,
    RenderedAlert,
    send_via_apprise,
)

# A realistic apprise URL: the token sits in the PATH, not in userinfo, which is
# exactly the shape that defeats naive "redact the password" scrubbing.
URL = "tgram://8412bottoken9931/551122chatid"
URL2 = "discord://webhook41255id/s3cretwebhooktoken"
ALERT = RenderedAlert(subject="[filearr] scan failed", body_text="1 library affected")


def _install_fake_apprise(
    monkeypatch,
    *,
    notify_result: bool = True,
    notify_exc: Exception | None = None,
    add_result: bool = True,
):
    """Inject a fake ``apprise`` module and return the call record.

    Models the real API surface the driver touches: ``Apprise()`` with ``add(url)
    -> bool`` and ``notify(title=, body=) -> bool``."""
    record: dict = {"added": [], "notified": [], "instances": 0}

    class _FakeApprise:
        def __init__(self) -> None:
            record["instances"] += 1

        def add(self, url):
            record["added"].append(url)
            return add_result

        def notify(self, **kwargs):
            record["notified"].append(kwargs)
            if notify_exc is not None:
                raise notify_exc
            return notify_result

    module = types.ModuleType("apprise")
    module.Apprise = _FakeApprise
    monkeypatch.setitem(sys.modules, "apprise", module)
    return record


# --------------------------------------------------------------------------- #
# missing optional dependency                                                 #
# --------------------------------------------------------------------------- #

async def test_missing_extra_is_permanent_and_names_the_extra(monkeypatch):
    # ``None`` in sys.modules makes ``import apprise`` raise ImportError, which is
    # how Python reports a module that is genuinely unavailable.
    monkeypatch.setitem(sys.modules, "apprise", None)
    with pytest.raises(ChannelDeliveryError) as ei:
        await send_via_apprise(URL, ALERT)
    # Non-retryable: an absent dependency cannot start working on retry, so it
    # must not consume the group's alert_max_delivery_attempts budget.
    assert ei.value.retryable is False
    assert "filearr[apprise]" in ei.value.detail


async def test_missing_extra_message_does_not_leak_the_url(monkeypatch):
    monkeypatch.setitem(sys.modules, "apprise", None)
    with pytest.raises(ChannelDeliveryError) as ei:
        await send_via_apprise(URL, ALERT)
    assert URL not in ei.value.detail


# --------------------------------------------------------------------------- #
# success                                                                     #
# --------------------------------------------------------------------------- #

async def test_success_returns_ok_and_passes_the_url_through(monkeypatch):
    record = _install_fake_apprise(monkeypatch)
    result = await send_via_apprise(URL, ALERT)
    assert result.ok is True
    # The URL must reach add() byte-for-byte: any mangling (stripping a query
    # string, splitting on a comma) silently destroys a valid credential.
    assert record["added"] == [URL]
    assert record["instances"] == 1


async def test_success_maps_subject_and_plaintext_body(monkeypatch):
    record = _install_fake_apprise(monkeypatch)
    await send_via_apprise(
        URL, RenderedAlert(subject="subj", body_text="plain body", payload={"a": 1})
    )
    assert record["notified"] == [{"title": "subj", "body": "plain body"}]
    # The webhook JSON payload is NOT what apprise gets — it would arrive as
    # literal JSON in a chat window.
    assert "{'a': 1}" not in str(record["notified"])


async def test_success_detail_does_not_leak_the_url(monkeypatch):
    _install_fake_apprise(monkeypatch)
    result = await send_via_apprise(URL, ALERT)
    assert URL not in result.detail


# --------------------------------------------------------------------------- #
# failure classification                                                      #
# --------------------------------------------------------------------------- #

async def test_notify_false_is_retryable(monkeypatch):
    _install_fake_apprise(monkeypatch, notify_result=False)
    with pytest.raises(ChannelDeliveryError) as ei:
        await send_via_apprise(URL, ALERT)
    assert ei.value.retryable is True
    # apprise swallows the per-service reason; the message must say where it went
    # rather than implying we know more than we do.
    assert "apprise" in ei.value.detail.lower()
    assert URL not in ei.value.detail


async def test_notify_exception_is_retryable_and_carries_the_reason(monkeypatch):
    _install_fake_apprise(monkeypatch, notify_exc=RuntimeError("connection reset by peer"))
    with pytest.raises(ChannelDeliveryError) as ei:
        await send_via_apprise(URL, ALERT)
    assert ei.value.retryable is True
    assert "connection reset by peer" in ei.value.detail


async def test_unparsable_url_is_permanent(monkeypatch):
    # add() returning False means apprise could not parse the URL / does not know
    # the scheme — permanent config error, distinct from a delivery failure.
    record = _install_fake_apprise(monkeypatch, add_result=False)
    with pytest.raises(ChannelDeliveryError) as ei:
        await send_via_apprise(URL, ALERT)
    assert ei.value.retryable is False
    assert record["notified"] == []  # never attempted the send
    assert URL not in ei.value.detail


async def test_blank_url_is_permanent_and_attempts_nothing(monkeypatch):
    record = _install_fake_apprise(monkeypatch)
    for blank in ("", "   ", "\n\n"):
        with pytest.raises(ChannelDeliveryError) as ei:
            await send_via_apprise(blank, ALERT)
        assert ei.value.retryable is False
        assert "url" in ei.value.detail.lower()
    # The blank check precedes the import and the client entirely.
    assert record["instances"] == 0
    assert record["added"] == []


async def test_blank_url_reported_before_the_missing_extra(monkeypatch):
    # A half-configured channel must name the field it is missing, not blame an
    # unrelated dependency it never needed to reach.
    monkeypatch.setitem(sys.modules, "apprise", None)
    with pytest.raises(ChannelDeliveryError) as ei:
        await send_via_apprise("", ALERT)
    assert "filearr[apprise]" not in ei.value.detail


# --------------------------------------------------------------------------- #
# multi-target channels                                                       #
# --------------------------------------------------------------------------- #

async def test_multiple_urls_each_get_their_own_add(monkeypatch):
    record = _install_fake_apprise(monkeypatch)
    result = await send_via_apprise(f"{URL}\n{URL2}", ALERT)
    assert result.ok is True
    assert record["added"] == [URL, URL2]
    assert record["instances"] == 1  # one Apprise object fans out to both
    assert len(record["notified"]) == 1  # one notify() call, not one per target


async def test_multiple_urls_tolerate_blank_lines_and_whitespace(monkeypatch):
    record = _install_fake_apprise(monkeypatch)
    await send_via_apprise(f"  {URL}  \n\n{URL2}\n", ALERT)
    assert record["added"] == [URL, URL2]


async def test_commas_inside_a_url_are_not_a_separator(monkeypatch):
    # Newline is the ONLY separator precisely because apprise URLs embed commas in
    # query parameters; splitting on them would corrupt this credential into two.
    record = _install_fake_apprise(monkeypatch)
    url = "mailto://user:pw@smtp.test?to=a@x.test,b@y.test&tags=ops,alerts"
    await send_via_apprise(url, ALERT)
    assert record["added"] == [url]


# --------------------------------------------------------------------------- #
# the URL is a credential: never logged, never in an error message            #
# --------------------------------------------------------------------------- #

async def test_url_never_appears_in_a_log_record_on_success(monkeypatch, caplog):
    _install_fake_apprise(monkeypatch)
    with caplog.at_level(logging.DEBUG, logger="filearr.alerts"):
        await send_via_apprise(f"{URL}\n{URL2}", ALERT)
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert URL not in blob and URL2 not in blob
    # The scheme IS logged (it is not secret) so an operator can still tell which
    # service a channel targets.
    assert "tgram" in blob and "discord" in blob


async def test_url_never_appears_in_a_log_record_on_failure(monkeypatch, caplog):
    _install_fake_apprise(monkeypatch, notify_exc=RuntimeError(f"POST {URL} failed"))
    with caplog.at_level(logging.DEBUG, logger="filearr.alerts"):
        with pytest.raises(ChannelDeliveryError):
            await send_via_apprise(URL, ALERT)
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert URL not in blob


async def test_exception_text_quoting_the_url_is_scrubbed(monkeypatch):
    # The realistic leak: a plugin raises with the URL (or a piece of it) in the
    # message, and that message would otherwise be persisted to
    # alert_events.last_error and served to API clients.
    _install_fake_apprise(monkeypatch, notify_exc=RuntimeError(f"failed to POST {URL}"))
    with pytest.raises(ChannelDeliveryError) as ei:
        await send_via_apprise(URL, ALERT)
    detail = ei.value.detail
    assert URL not in detail
    assert "8412bottoken9931" not in detail  # nor the bare token component
    assert "failed to POST" in detail  # the useful part survives
    assert "redacted" in detail


async def test_partial_url_fragment_is_scrubbed(monkeypatch):
    # Plugins often raise with only the credential component, so full-string
    # replacement alone would not be enough.
    _install_fake_apprise(
        monkeypatch, notify_exc=RuntimeError("bot 8412bottoken9931 is not a member")
    )
    with pytest.raises(ChannelDeliveryError) as ei:
        await send_via_apprise(URL, ALERT)
    assert "8412bottoken9931" not in ei.value.detail


async def test_long_exception_cannot_leave_a_truncated_url_behind(monkeypatch):
    # sanitize_error truncates at MAX_ERROR_CHARS; scrubbing therefore runs on the
    # RAW text first, or a URL straddling the cut would leave a still-secret
    # prefix in the stored error forever.
    from filearr.errors import MAX_ERROR_CHARS

    noise = "x" * (MAX_ERROR_CHARS - 10)
    _install_fake_apprise(monkeypatch, notify_exc=RuntimeError(f"{noise}{URL}"))
    with pytest.raises(ChannelDeliveryError) as ei:
        await send_via_apprise(URL, ALERT)
    # No non-trivial prefix of the token survives the truncation boundary.
    assert "8412bottoken" not in ei.value.detail


# --------------------------------------------------------------------------- #
# end-to-end through the pump's channel router (decrypt -> dispatch)          #
# --------------------------------------------------------------------------- #

async def test_send_to_channel_routes_apprise_end_to_end(monkeypatch):
    """The pump's ``_send_to_channel`` decrypts the whole-URL secret and hands the
    PLAINTEXT to the driver — the P8-T4 boundary (crypto) meeting P8-T3."""
    from filearr.alerts import crypto
    from filearr.alerts.dispatch import encrypt_channel_secret
    from filearr.config import get_settings
    from filearr.models import AlertChannel
    from filearr.tasks.alerts import _send_to_channel

    settings = get_settings()
    monkeypatch.setattr(settings, "secret_key", "unit-test-secret-key")
    key = crypto.require_content_key()

    record = _install_fake_apprise(monkeypatch)
    channel = AlertChannel(
        name="ops-telegram",
        type_="apprise",
        config={"url": encrypt_channel_secret(URL, key)},
        dispatch_locality="central",
        enabled=True,
    )
    await _send_to_channel(channel, ALERT, settings)

    assert record["added"] == [URL]  # decrypted plaintext, intact
    assert record["notified"] == [
        {"title": ALERT.subject, "body": ALERT.body_text}
    ]
    # The stored config is still ciphertext — decryption is in-process only.
    assert channel.config["url"] != URL


async def test_send_to_channel_surfaces_missing_extra_as_permanent(monkeypatch):
    from filearr.alerts import crypto
    from filearr.alerts.dispatch import encrypt_channel_secret
    from filearr.config import get_settings
    from filearr.models import AlertChannel
    from filearr.tasks.alerts import _send_to_channel

    settings = get_settings()
    monkeypatch.setattr(settings, "secret_key", "unit-test-secret-key")
    key = crypto.require_content_key()
    monkeypatch.setitem(sys.modules, "apprise", None)

    channel = AlertChannel(
        name="ops-telegram",
        type_="apprise",
        config={"url": encrypt_channel_secret(URL, key)},
        dispatch_locality="central",
        enabled=True,
    )
    with pytest.raises(ChannelDeliveryError) as ei:
        await _send_to_channel(channel, ALERT, settings)
    # Terminal on the first attempt (the pump marks it failed, never retries) and
    # actionable for the operator reading alert_events.last_error.
    assert ei.value.retryable is False
    assert "filearr[apprise]" in ei.value.detail
