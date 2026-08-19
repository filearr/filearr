"""E-mail extraction (roadmap §15 / §5, 2026-08-19): ``.eml`` messages,
``.mbox`` mailboxes and Outlook ``.msg`` files.

Everything here is index-only and bounded: a size ceiling before any parse, a
message cap for mailboxes, and the same ``body_text`` character cap the
document extractors use. Parsers are the stdlib ``email`` / ``mailbox`` modules
(RFC 822 / mbox) and ``olefile`` (the OLE compound container an Outlook .msg
is). Headers and bodies are untrusted: every string is control-stripped and
length-capped, HTML bodies are reduced to text with the stdlib parser (no
script/style content, no network), and nothing is ever written.

Emitted (flat, into ``metadata_``; all optional):

    email_subject, email_from, email_to, email_cc, email_date (ISO-8601),
    email_message_id, email_attachments (list[str], capped),
    email_attachment_count, body_text, body_text_truncated
    mailbox: {message_count, first_date, last_date, truncated}
    unsupported: True  (+ unsupported_reason)  for PST/OST and unknown kinds

PST / OST are deliberately NOT parsed: the only robust reader is libpff (a
native dependency). Convert with ``readpst -r`` (libpff/pst-utils) to mbox and
point a library at the output.
"""

from __future__ import annotations

import email
import email.policy
import email.utils
import mailbox
import os
from datetime import UTC
from html.parser import HTMLParser
from pathlib import PurePath
from typing import Any

DEFAULT_MAX_BYTES = 256 * 1024 * 1024  # one .eml/.msg/.mbox handed to a parser
DEFAULT_MBOX_MAX_MESSAGES = 5000
DEFAULT_BODY_MAX_CHARS = 100_000
_ATTACHMENTS_CAP = 100
_HEADER_CAP = 1000

_MESSAGE_EXTS = frozenset({"eml", "emlx", "mht", "mhtml", "mim", "mime", "nws"})
_MBOX_EXTS = frozenset({"mbox", "mbx", "mbs"})
_MSG_EXTS = frozenset({"msg", "oft"})
_UNSUPPORTED_EXTS = {
    "pst": "Outlook PST needs libpff (native); convert with readpst to mbox",
    "ost": "Outlook OST needs libpff (native); convert with readpst to mbox",
    "dbx": "Outlook Express DBX is not supported",
    "tnef": "TNEF (winmail.dat) is not supported",
    "p7m": "S/MIME envelope is not parsed",
}


class EmailError(RuntimeError):
    def __init__(self, message: str, *, kind: str = "corrupt") -> None:
        super().__init__(message)
        self.kind = kind


def _clean(s: object, cap: int = _HEADER_CAP) -> str | None:
    if s is None:
        return None
    text = str(s)
    text = "".join(ch for ch in text if ch >= " " or ch in "\n\t").strip()
    if not text:
        return None
    return text[:cap]


def _date_iso(raw: object) -> str | None:
    if not raw:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(str(raw))
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


class _TextExtract(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):  # noqa: D401
        if tag in ("script", "style", "head"):
            self._skip += 1
        elif tag in ("br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head") and self._skip:
            self._skip -= 1
        elif tag in ("p", "div", "tr", "li"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    p = _TextExtract()
    try:
        p.feed(html)
        p.close()
    except Exception:  # noqa: BLE001 - hostile markup: keep what we have
        pass
    text = "".join(p.parts)
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _body_of(msg: email.message.Message, max_chars: int) -> tuple[str | None, bool]:
    """Best text body: first text/plain part, else first text/html reduced."""
    plain = None
    html = None
    try:
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                disp = str(part.get("Content-Disposition") or "").lower()
                if "attachment" in disp:
                    continue
                if ctype == "text/plain" and plain is None:
                    plain = _payload_text(part)
                elif ctype == "text/html" and html is None:
                    html = _payload_text(part)
                if plain is not None:
                    break
        else:
            ctype = msg.get_content_type()
            if ctype == "text/plain":
                plain = _payload_text(msg)
            elif ctype == "text/html":
                html = _payload_text(msg)
    except Exception:  # noqa: BLE001 - malformed MIME tree
        pass
    text = plain if plain is not None else (html_to_text(html) if html else None)
    if not text:
        return None, False
    text = "".join(ch for ch in text if ch >= " " or ch in "\n\t")
    if len(text) > max_chars:
        return text[:max_chars], True
    return text, False


def _payload_text(part: email.message.Message) -> str | None:
    try:
        payload = part.get_payload(decode=True)
    except Exception:  # noqa: BLE001
        return None
    if payload is None:
        return None
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _attachments(msg: email.message.Message) -> list[str]:
    names: list[str] = []
    try:
        for part in msg.walk():
            if len(names) >= _ATTACHMENTS_CAP:
                break
            fn = part.get_filename()
            disp = str(part.get("Content-Disposition") or "").lower()
            if fn and ("attachment" in disp or "inline" in disp or not part.is_multipart()):
                c = _clean(fn, 255)
                if c and c not in names:
                    names.append(c)
    except Exception:  # noqa: BLE001
        pass
    return names


def _headers(msg: email.message.Message, out: dict[str, Any]) -> None:
    def put(key: str, val: object) -> None:
        c = _clean(val)
        if c:
            out[key] = c

    put("email_subject", msg.get("Subject"))
    put("email_from", msg.get("From"))
    put("email_to", msg.get("To"))
    put("email_cc", msg.get("Cc"))
    put("email_message_id", msg.get("Message-ID"))
    if (d := _date_iso(msg.get("Date"))) is not None:
        out["email_date"] = d


def extract_eml(path: str, *, max_chars: int) -> dict[str, Any]:
    try:
        with open(path, "rb") as fh:
            msg = email.message_from_binary_file(fh, policy=email.policy.compat32)
    except OSError as exc:
        raise EmailError(f"cannot read message: {exc}", kind="error") from exc
    except Exception as exc:  # noqa: BLE001
        raise EmailError(f"not a parseable message: {type(exc).__name__}") from exc
    if not msg.keys():
        # The stdlib parser accepts ANY bytes as a headerless body; a file with
        # no RFC 822 headers at all is not a message.
        raise EmailError("not a parseable message: no headers")
    out: dict[str, Any] = {}
    _headers(msg, out)
    atts = _attachments(msg)
    if atts:
        out["email_attachments"] = atts
    out["email_attachment_count"] = len(atts)
    body, truncated = _body_of(msg, max_chars)
    if body:
        out["body_text"] = body
        out["body_text_truncated"] = truncated
    if not out.get("email_subject") and not out.get("email_from") and not body:
        raise EmailError("no message headers or body found")
    return out


def extract_mbox(path: str, *, max_chars: int, max_messages: int) -> dict[str, Any]:
    try:
        box = mailbox.mbox(path, create=False)
    except OSError as exc:
        raise EmailError(f"cannot open mailbox: {exc}", kind="error") from exc
    count = 0
    truncated = False
    first: str | None = None
    last: str | None = None
    lines: list[str] = []
    chars = 0
    try:
        for msg in box:
            if count >= max_messages:
                truncated = True
                break
            count += 1
            d = _date_iso(msg.get("Date"))
            if d:
                first = d if first is None or d < first else first
                last = d if last is None or d > last else last
            if chars < max_chars:
                subj = _clean(msg.get("Subject"), 300) or "(no subject)"
                frm = _clean(msg.get("From"), 200) or ""
                line = f"{subj} — {frm}" if frm else subj
                lines.append(line)
                chars += len(line) + 1
    except Exception as exc:  # noqa: BLE001 - a corrupt message mid-box
        if count == 0:
            raise EmailError(f"not a parseable mailbox: {type(exc).__name__}") from exc
        truncated = True
    finally:
        try:
            box.close()
        except Exception:  # noqa: BLE001
            pass
    out: dict[str, Any] = {
        "mailbox": {
            "message_count": count,
            "first_date": first,
            "last_date": last,
            "truncated": truncated,
        }
    }
    if lines:
        text = "\n".join(lines)
        out["body_text"] = text[:max_chars]
        out["body_text_truncated"] = truncated or len(text) > max_chars
    return out


# Outlook .msg: MAPI property streams inside the OLE container. 001F = UTF-16LE,
# 001E = 8-bit (code page unknown; decoded permissively).
_MSG_PROPS = {
    "0037": "email_subject",  # PR_SUBJECT
    "0C1A": "email_from",  # PR_SENDER_NAME
    "0E04": "email_to",  # PR_DISPLAY_TO
    "0E03": "email_cc",  # PR_DISPLAY_CC
    "1035": "email_message_id",  # PR_INTERNET_MESSAGE_ID
}


def _ole_text(ole, name: str) -> str | None:
    for suffix, enc in (("001F", "utf-16-le"), ("001E", "cp1252")):
        stream = f"__substg1.0_{name}{suffix}"
        if ole.exists(stream):
            try:
                raw = ole.openstream(stream).read()
            except Exception:  # noqa: BLE001
                return None
            return raw.decode(enc, errors="replace")
    return None


def extract_msg(path: str, *, max_chars: int) -> dict[str, Any]:
    try:
        import olefile
    except ImportError as exc:  # pragma: no cover - pinned
        raise EmailError("olefile not installed", kind="error") from exc
    try:
        if not olefile.isOleFile(path):
            raise EmailError("not an OLE compound file (not an Outlook .msg)")
        ole = olefile.OleFileIO(path)
    except EmailError:
        raise
    except OSError as exc:
        raise EmailError(f"cannot read .msg: {exc}", kind="error") from exc
    except Exception as exc:  # noqa: BLE001
        raise EmailError(f"not a parseable .msg: {type(exc).__name__}") from exc
    out: dict[str, Any] = {}
    try:
        for prop, key in _MSG_PROPS.items():
            c = _clean(_ole_text(ole, prop))
            if c:
                out[key] = c
        # transport headers carry the Date; the sender e-mail is PR_SENDER_EMAIL_ADDRESS
        if not out.get("email_from"):
            c = _clean(_ole_text(ole, "0C1F"))
            if c:
                out["email_from"] = c
        headers = _ole_text(ole, "007D")
        if headers:
            try:
                hm = email.message_from_string(headers, policy=email.policy.compat32)
                if (d := _date_iso(hm.get("Date"))) is not None:
                    out["email_date"] = d
                if not out.get("email_message_id"):
                    c = _clean(hm.get("Message-ID"))
                    if c:
                        out["email_message_id"] = c
            except Exception:  # noqa: BLE001
                pass
        body = _ole_text(ole, "1000")  # PR_BODY
        if not body:
            html = _ole_text(ole, "1013")  # PR_HTML (8-bit usually)
            if html:
                body = html_to_text(html)
        if body:
            body = "".join(ch for ch in body if ch >= " " or ch in "\n\t").strip()
            if body:
                out["body_text"] = body[:max_chars]
                out["body_text_truncated"] = len(body) > max_chars
        # attachments: __attach_version1.0_#00000000/__substg1.0_3707001F (long name)
        names: list[str] = []
        for entry in ole.listdir(streams=False, storages=True):
            if len(entry) == 1 and entry[0].startswith("__attach_version1.0_"):
                for prop in ("3707", "3704"):
                    for suffix, enc in (("001F", "utf-16-le"), ("001E", "cp1252")):
                        st = [entry[0], f"__substg1.0_{prop}{suffix}"]
                        if ole.exists(st):
                            try:
                                nm = _clean(ole.openstream(st).read().decode(enc, "replace"), 255)
                            except Exception:  # noqa: BLE001
                                nm = None
                            if nm and nm not in names:
                                names.append(nm)
                            break
                    else:
                        continue
                    break
                if len(names) >= _ATTACHMENTS_CAP:
                    break
        if names:
            out["email_attachments"] = names
        out["email_attachment_count"] = len(names)
    finally:
        try:
            ole.close()
        except Exception:  # noqa: BLE001
            pass
    if not out.get("email_subject") and not out.get("body_text"):
        raise EmailError("no MAPI subject/body streams found")
    return out


def extract_email(
    path: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_chars: int = DEFAULT_BODY_MAX_CHARS,
    max_messages: int = DEFAULT_MBOX_MAX_MESSAGES,
) -> dict[str, Any]:
    """Dispatch by extension. Raises :class:`EmailError` on parse failure."""
    ext = PurePath(path).suffix.lstrip(".").lower()
    if ext in _UNSUPPORTED_EXTS:
        return {"unsupported": True, "unsupported_reason": _UNSUPPORTED_EXTS[ext]}
    if ext not in _MESSAGE_EXTS and ext not in _MBOX_EXTS and ext not in _MSG_EXTS:
        return {"unsupported": True}
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise EmailError(f"cannot stat: {exc}", kind="error") from exc
    if size > max_bytes:
        raise EmailError(f"e-mail file too large ({size} > {max_bytes} bytes)", kind="guard")
    if ext in _MBOX_EXTS:
        return extract_mbox(path, max_chars=max_chars, max_messages=max_messages)
    if ext in _MSG_EXTS:
        return extract_msg(path, max_chars=max_chars)
    return extract_eml(path, max_chars=max_chars)
