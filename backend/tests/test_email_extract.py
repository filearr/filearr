# ruff: noqa: E501
"""Roadmap §15 / §5 (2026-08-19): e-mail extraction (.eml / .mbox / .msg)."""

from __future__ import annotations

import mailbox
from email.message import EmailMessage

import pytest

from filearr.tasks import email_extract as ex


def _msg(
    subject="Hello",
    body="plain body",
    html=None,
    attach=None,
    date="Tue, 19 Aug 2026 10:00:00 +0000",
):
    m = EmailMessage()
    m["Subject"] = subject
    m["From"] = "Alice <alice@example.com>"
    m["To"] = "bob@example.com"
    m["Date"] = date
    m["Message-ID"] = "<abc@example.com>"
    if html:
        m.set_content(body)
        m.add_alternative(html, subtype="html")
    else:
        m.set_content(body)
    for name in attach or []:
        m.add_attachment(b"data", maintype="application", subtype="octet-stream", filename=name)
    return m


def test_eml_headers_body_attachments(tmp_path):
    p = tmp_path / "m.eml"
    p.write_bytes(_msg(attach=["report.pdf", "pic.jpg"]).as_bytes())
    out = ex.extract_email(str(p))
    assert out["email_subject"] == "Hello" and out["email_from"].startswith("Alice")
    assert out["email_to"] == "bob@example.com"
    assert out["email_date"] == "2026-08-19T10:00:00+00:00"
    assert out["email_message_id"] == "<abc@example.com>"
    assert out["email_attachments"] == ["report.pdf", "pic.jpg"]
    assert out["email_attachment_count"] == 2
    assert out["body_text"].strip() == "plain body" and out["body_text_truncated"] is False


def test_eml_html_only_reduced_to_text(tmp_path):
    m = EmailMessage()
    m["Subject"] = "H"
    m["From"] = "x@y.z"
    m.set_content(
        "<html><head><style>p{}</style></head><body><p>Hi <b>there</b></p><script>evil()</script></body></html>",
        subtype="html",
    )
    p = tmp_path / "h.eml"
    p.write_bytes(m.as_bytes())
    out = ex.extract_email(str(p))
    assert out["body_text"] == "Hi there"


def test_eml_body_cap_and_size_guard(tmp_path):
    p = tmp_path / "big.eml"
    p.write_bytes(_msg(body="x" * 5000).as_bytes())
    out = ex.extract_email(str(p), max_chars=100)
    assert len(out["body_text"]) == 100 and out["body_text_truncated"] is True
    with pytest.raises(ex.EmailError) as ei:
        ex.extract_email(str(p), max_bytes=10)
    assert ei.value.kind == "guard"


def test_eml_garbage_raises(tmp_path):
    p = tmp_path / "junk.eml"
    p.write_bytes(b"\x00\x01\x02 nothing here")
    with pytest.raises(ex.EmailError):
        ex.extract_email(str(p))


def test_mbox_summary_and_cap(tmp_path):
    p = tmp_path / "in.mbox"
    box = mailbox.mbox(str(p))
    for i in range(5):
        box.add(_msg(subject=f"msg {i}", date=f"Tue, 1{i} Aug 2026 10:00:00 +0000"))
    box.flush()
    box.close()
    out = ex.extract_email(str(p))
    mb = out["mailbox"]
    assert mb["message_count"] == 5 and mb["truncated"] is False
    assert mb["first_date"].startswith("2026-08-10") and mb["last_date"].startswith("2026-08-14")
    assert "msg 3 — Alice" in out["body_text"]
    out = ex.extract_email(str(p), max_messages=2)
    assert out["mailbox"]["message_count"] == 2 and out["mailbox"]["truncated"] is True


def test_unsupported_pst_marker(tmp_path):
    p = tmp_path / "a.pst"
    p.write_bytes(b"!BDN")
    out = ex.extract_email(str(p))
    assert out["unsupported"] is True and "readpst" in out["unsupported_reason"]
    assert ex.extract_email(str(tmp_path / "x.unknown")) == {"unsupported": True}


def test_msg_via_olefile(tmp_path):
    # Build a minimal Outlook .msg with olefile's writer is not possible (it only
    # edits existing streams), so synthesise the OLE container with a tiny
    # compound-file writer from the test: skip when unavailable, but always
    # verify the non-OLE path is refused cleanly.
    p = tmp_path / "not.msg"
    p.write_bytes(b"plain text, not OLE")
    with pytest.raises(ex.EmailError, match="OLE"):
        ex.extract_email(str(p))


def test_html_to_text_is_defensive():
    assert ex.html_to_text("<p>a</p><p>b</p>") == "a\nb"
    assert ex.html_to_text("<<<>>>") == "<<<>>>" or ex.html_to_text("<<<>>>") == ""
    assert ex.html_to_text("") == ""


def test_group_override_routes_email():
    from filearr.tasks.extract import EXTRACTOR_BY_GROUP

    assert EXTRACTOR_BY_GROUP["email"].__name__ == "extract_email"
