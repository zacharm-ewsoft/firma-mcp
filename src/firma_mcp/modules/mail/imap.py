"""Reading mail over IMAP without changing anything in the mailbox.

The folder is opened with EXAMINE (read-only) and messages are fetched with BODY.PEEK, so
nothing is ever marked as read.
"""

from __future__ import annotations

import base64
import contextlib
import datetime as dt
import html as html_lib
import io
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from html.parser import HTMLParser

from docx import Document
from imap_tools import AND, MailBox
from imap_tools.errors import ImapToolsError, MailboxLoginError
from pypdf import PdfReader

from firma_mcp.errors import SourceError

IMAP_TIMEOUT = 20
SOURCE = "mail"
ATTACHMENT_TEXT_MAX_CHARS = 60_000
ATTACHMENT_BASE64_MAX_BYTES = 2 * 1024 * 1024
MAX_LIST_LIMIT = 200


@dataclass(frozen=True)
class MailConfig:
    host: str
    port: int
    boxes: tuple[str, ...]
    passwords: dict[str, str]

    def password(self, box: str) -> str:
        try:
            return self.passwords[box]
        except KeyError:
            raise ValueError(f"unknown mailbox: {box}") from None


class _TextExtractor(HTMLParser):
    SKIP = {"style", "script", "head"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "table"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip_depth:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    text = html_lib.unescape("".join(parser.parts)).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _body_text(msg) -> str:
    text = (getattr(msg, "text", "") or "").strip()
    if text:
        return text
    return html_to_text(getattr(msg, "html", "") or "")


def _single_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def summarize(msg, box: str, preview_chars: int = 200) -> dict:
    body = _body_text(msg)
    date = getattr(msg, "date", None)
    return {
        "uid": msg.uid,
        "box": box,
        "date": date.isoformat() if isinstance(date, dt.datetime) else None,
        "from": msg.from_,
        "subject": msg.subject,
        "preview": _single_line(body)[:preview_chars],
        "attachments": [{"filename": a.filename, "size": a.size} for a in msg.attachments],
        "seen": "\\Seen" in tuple(msg.flags),
    }


def full_message(msg, box: str, max_chars: int = 20000) -> dict:
    out = summarize(msg, box)
    body = _body_text(msg)
    out["to"] = list(msg.to)
    out["text"] = body[:max_chars]
    out["truncated"] = len(body) > max_chars
    return out


@contextmanager
def open_box(config: MailConfig, box: str) -> Iterator[MailBox]:
    password = config.password(box)
    mailbox = MailBox(config.host, config.port, timeout=IMAP_TIMEOUT)
    # initial_folder=None stops login() from opening INBOX with SELECT (read-write).
    # We open it ourselves with EXAMINE (readonly=True).
    mailbox.login(box, password, initial_folder=None)
    mailbox.folder.set("INBOX", readonly=True)
    try:
        yield mailbox
    finally:
        # A failing logout must not hide the actual result.
        with contextlib.suppress(Exception):
            mailbox.logout()


MailboxFactory = Callable[[MailConfig, str], object]


def _validate_box(config: MailConfig, box: str) -> None:
    if box not in config.boxes:
        raise SourceError(SOURCE, f"unknown mailbox: {box}. Available: {', '.join(config.boxes)}")


@contextmanager
def _source_errors(config: MailConfig, box: str) -> Iterator[None]:
    """Turn connection, login and IMAP failures into SourceError."""
    try:
        yield
    except MailboxLoginError as exc:
        raise SourceError(SOURCE, f"login to {box} rejected: {exc}") from exc
    except ImapToolsError as exc:
        raise SourceError(SOURCE, f"IMAP error ({type(exc).__name__}) for {box}: {exc}") from exc
    except (OSError, TimeoutError) as exc:
        raise SourceError(SOURCE, f"IMAP connection to {config.host} failed: {exc}") from exc


def list_messages(
    config: MailConfig, box: str, days: int, *, mailbox_factory: MailboxFactory = open_box, limit: int = 40
) -> list[dict]:
    _validate_box(config, box)
    limit = max(1, min(limit, MAX_LIST_LIMIT))
    since = dt.date.today() - dt.timedelta(days=days)
    criteria = AND(date_gte=since)
    with _source_errors(config, box), mailbox_factory(config, box) as mailbox:
        return [
            summarize(m, box) for m in mailbox.fetch(criteria, mark_seen=False, reverse=True, limit=limit)
        ]


def get_message(
    config: MailConfig, box: str, uid: str, *, mailbox_factory: MailboxFactory = open_box
) -> dict:
    _validate_box(config, box)
    with _source_errors(config, box), mailbox_factory(config, box) as mailbox:
        for m in mailbox.fetch(mark_seen=False, uid_list=[str(uid)]):
            return full_message(m, box)
    raise SourceError(SOURCE, f"message uid={uid} not found in {box}")


def _find_attachment(msg, filename: str):
    attachments = list(msg.attachments)
    for a in attachments:
        if a.filename == filename:
            return a
    lower = filename.lower()
    for a in attachments:
        if (a.filename or "").lower() == lower:
            return a
    available = ", ".join(a.filename for a in attachments if a.filename) or "no attachments"
    raise SourceError(SOURCE, f"attachment '{filename}' not found; available: {available}")


def _extract_pdf_text(payload: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(payload))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:  # noqa: BLE001 - a broken PDF is a source error, not a traceback
        raise SourceError(SOURCE, f"could not read the PDF: {exc}") from exc


def _extract_docx_text(payload: bytes) -> str:
    try:
        doc = Document(io.BytesIO(payload))
        parts = [p.text for p in doc.paragraphs]
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    parts.append(cell.text)
        return "\n".join(part for part in parts if part)
    except Exception as exc:  # noqa: BLE001 - same as for PDF
        raise SourceError(SOURCE, f"could not read the DOCX: {exc}") from exc


def _extract_text(filename: str, content_type: str, payload: bytes) -> str:
    name = (filename or "").lower()
    ctype = (content_type or "").lower()
    if name.endswith(".pdf") or "pdf" in ctype:
        return _extract_pdf_text(payload)
    if name.endswith(".docx") or "wordprocessingml" in ctype:
        return _extract_docx_text(payload)
    if name.endswith(".txt") or ctype.startswith("text/"):
        return payload.decode("utf-8", errors="replace")
    raise SourceError(SOURCE, f"no text extraction for type {content_type or filename}; use mode=base64")


def get_attachment(
    config: MailConfig,
    box: str,
    uid: str,
    filename: str,
    mode: str = "text",
    *,
    mailbox_factory: MailboxFactory = open_box,
) -> dict:
    _validate_box(config, box)
    if mode not in ("text", "base64"):
        raise SourceError(SOURCE, f"unknown mode={mode!r}; allowed: 'text', 'base64'")
    with _source_errors(config, box), mailbox_factory(config, box) as mailbox:
        for m in mailbox.fetch(mark_seen=False, uid_list=[str(uid)]):
            attachment = _find_attachment(m, filename)
            payload = attachment.payload or b""
            content_type = getattr(attachment, "content_type", None)
            out = {
                "uid": str(uid),
                "box": box,
                "filename": attachment.filename,
                "content_type": content_type,
                "size": len(payload),
                "mode": mode,
            }
            if mode == "base64":
                if len(payload) > ATTACHMENT_BASE64_MAX_BYTES:
                    raise SourceError(
                        SOURCE,
                        f"attachment '{attachment.filename}' is too large for base64 "
                        f"({len(payload)} B > {ATTACHMENT_BASE64_MAX_BYTES} B)",
                    )
                out["data"] = base64.b64encode(payload).decode("ascii")
                out["truncated"] = False
            else:
                text = _extract_text(attachment.filename, content_type, payload)
                out["text"] = text[:ATTACHMENT_TEXT_MAX_CHARS]
                out["truncated"] = len(text) > ATTACHMENT_TEXT_MAX_CHARS
            return out
    raise SourceError(SOURCE, f"message uid={uid} not found in {box}")
