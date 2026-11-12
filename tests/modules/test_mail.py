import datetime as dt
import io
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from docx import Document
from imap_tools.errors import MailboxFetchError, MailboxFolderSelectError
from pypdf import PdfWriter

from firma_mcp.errors import SourceError
from firma_mcp.modules.mail import imap as mail
from firma_mcp.modules.mail import load


def fake_msg(
    uid="7",
    subject="Invoice 12/2026",
    text="Hello,\nthe invoice is attached.",
    html="",
    attachments=(),
    flags=(),
):
    return SimpleNamespace(
        uid=uid,
        subject=subject,
        from_="accounting@example.test",
        to=("office@example.com",),
        date=dt.datetime(2026, 9, 9, 10, 30, tzinfo=dt.UTC),
        text=text,
        html=html,
        flags=flags,
        attachments=[SimpleNamespace(filename=n, size=s) for n, s in attachments],
    )


class FakeBox:
    def __init__(self, messages):
        self.messages = messages
        self.calls = []

    def fetch(self, criteria="ALL", *, mark_seen=True, reverse=False, limit=None, uid_list=None, bulk=False):
        self.calls.append(
            {
                "criteria": criteria,
                "mark_seen": mark_seen,
                "reverse": reverse,
                "limit": limit,
                "uid_list": uid_list,
            }
        )
        msgs = self.messages
        if uid_list is not None:
            msgs = [m for m in msgs if m.uid in set(uid_list)]
        return iter(msgs)


def factory_for(box_obj):
    @contextmanager
    def factory(settings, box):
        yield box_obj

    return factory


class FakeFolder:
    def __init__(self):
        self.calls = []

    def set(self, folder, readonly=False):
        self.calls.append((folder, readonly))


class FakeMailBox:
    """Stands in for imap_tools.MailBox without any network connection."""

    instances = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.folder = FakeFolder()
        self.login_calls = []
        self.logout_called = False
        FakeMailBox.instances.append(self)

    def login(self, username, password, initial_folder="INBOX"):
        self.login_calls.append(
            {"username": username, "password": password, "initial_folder": initial_folder}
        )
        return self

    def logout(self):
        self.logout_called = True


@pytest.fixture
def settings(env):
    return load(env)


def test_html_to_text_strips_tags_and_collapses_whitespace():
    html = "<html><body><p>Hello&nbsp;<b>world</b></p><style>x{}</style>\n\n<div>Next</div></body></html>"
    assert mail.html_to_text(html) == "Hello world\nNext"


def test_summarize_prefers_text_and_truncates_preview():
    msg = fake_msg(text="a" * 600, attachments=(("invoice.pdf", 12345),))
    out = mail.summarize(msg, "office@example.com", preview_chars=100)
    assert out["uid"] == "7"
    assert out["box"] == "office@example.com"
    assert out["date"] == "2026-09-09T10:30:00+00:00"
    assert out["from"] == "accounting@example.test"
    assert out["subject"] == "Invoice 12/2026"
    assert len(out["preview"]) == 100
    assert out["attachments"] == [{"filename": "invoice.pdf", "size": 12345}]
    assert out["seen"] is False


def test_summarize_default_listing_has_no_to_and_single_line_preview():
    msg = fake_msg(text="Line one\n\nLine   two\ttab")
    out = mail.summarize(msg, "office@example.com")
    assert "to" not in out
    assert out["preview"] == "Line one Line two tab"
    assert "\n" not in out["preview"]


def test_summarize_falls_back_to_html():
    msg = fake_msg(text="", html="<p>Only <i>HTML</i></p>", flags=("\\Seen",))
    out = mail.summarize(msg, "office@example.com")
    assert out["preview"] == "Only HTML"
    assert out["seen"] is True


def test_full_message_truncates_and_flags():
    msg = fake_msg(text="x" * 30)
    out = mail.full_message(msg, "office@example.com", max_chars=10)
    assert out["text"] == "x" * 10
    assert out["truncated"] is True
    assert "preview" in out
    assert out["to"] == ["office@example.com"]


def test_list_messages_never_marks_seen_and_uses_since(settings):
    box = FakeBox([fake_msg(uid="1"), fake_msg(uid="2")])
    out = mail.list_messages(settings, "office@example.com", days=3, mailbox_factory=factory_for(box))
    assert [m["uid"] for m in out] == ["1", "2"]
    call = box.calls[0]
    assert call["mark_seen"] is False
    assert call["reverse"] is True
    assert call["limit"] == 40
    assert "SINCE" in call["criteria"]


@pytest.mark.parametrize("given,expected", [(0, 1), (-5, 1), (1, 1), (200, 200), (999, 200)])
def test_list_messages_clamps_limit(settings, given, expected):
    box = FakeBox([fake_msg(uid="1")])
    mail.list_messages(settings, "office@example.com", days=3, mailbox_factory=factory_for(box), limit=given)
    assert box.calls[0]["limit"] == expected


def test_list_messages_unknown_box_is_source_error(settings):
    with pytest.raises(SourceError) as exc:
        mail.list_messages(settings, "someone@example.test", days=3, mailbox_factory=factory_for(FakeBox([])))
    assert exc.value.source == "mail"


def test_get_message_by_uid(settings):
    box = FakeBox([fake_msg(uid="1"), fake_msg(uid="2", subject="Druga")])
    out = mail.get_message(settings, "office@example.com", "2", mailbox_factory=factory_for(box))
    assert out["subject"] == "Druga"
    assert box.calls[0]["mark_seen"] is False
    assert box.calls[0]["uid_list"] == ["2"]


def test_get_message_missing_uid(settings):
    box = FakeBox([fake_msg(uid="1")])
    with pytest.raises(SourceError, match="not found"):
        mail.get_message(settings, "office@example.com", "99", mailbox_factory=factory_for(box))


def test_connection_error_becomes_source_error(settings):
    @contextmanager
    def broken(settings, box):
        raise OSError("connection refused")
        yield  # pragma: no cover

    with pytest.raises(SourceError, match="connection refused"):
        mail.list_messages(settings, "office@example.com", days=1, mailbox_factory=broken)


class RaisingFetchBox:
    def fetch(self, *args, **kwargs):
        raise MailboxFetchError(("NO", [b"fetch failed"]), "OK")


def test_list_messages_wraps_imap_tools_error(settings):
    with pytest.raises(SourceError) as exc:
        mail.list_messages(
            settings, "office@example.com", days=1, mailbox_factory=factory_for(RaisingFetchBox())
        )
    assert exc.value.source == "mail"


def test_open_box_selects_inbox_readonly_and_logs_out(settings, monkeypatch):
    FakeMailBox.instances.clear()
    monkeypatch.setattr(mail, "MailBox", FakeMailBox)
    with mail.open_box(settings, "office@example.com") as box:
        assert isinstance(box, FakeMailBox)
    fake = FakeMailBox.instances[0]
    assert fake.login_calls[0]["initial_folder"] is None
    assert fake.folder.calls == [("INBOX", True)]
    assert fake.logout_called is True


def test_get_message_wraps_folder_select_error(settings):
    @contextmanager
    def broken_folder(settings, box):
        raise MailboxFolderSelectError(("NO", [b"select failed"]), "OK")
        yield  # pragma: no cover

    with pytest.raises(SourceError) as exc:
        mail.get_message(settings, "office@example.com", "1", mailbox_factory=broken_folder)
    assert exc.value.source == "mail"


# -- mail_attachment ---------------------------------------------------------


def fake_attachment(filename, payload, content_type="application/octet-stream"):
    return SimpleNamespace(filename=filename, payload=payload, content_type=content_type, size=len(payload))


def fake_msg_with_attachments(uid, attachments):
    return SimpleNamespace(uid=uid, attachments=list(attachments))


def make_pdf_bytes() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def make_text_pdf_bytes(text: str) -> bytes:
    """A hand-assembled minimal one-page PDF with real text. pypdf.PdfWriter cannot write
    text, so the content stream and the xref table (with correct offsets) are built by hand."""
    content = f"BT /F1 12 Tf 72 700 Td ({text}) Tj ET".encode("latin-1")
    buf = bytearray(b"%PDF-1.4\n")
    offsets = [0]

    def add_obj(num: int, body: bytes) -> None:
        offsets.append(len(buf))
        buf.extend(f"{num} 0 obj\n".encode("latin-1"))
        buf.extend(body)
        buf.extend(b"\nendobj\n")

    add_obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    add_obj(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    add_obj(
        3,
        b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> "
        b"/MediaBox [0 0 612 792] /Contents 5 0 R >>",
    )
    add_obj(4, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    stream_body = (
        b"<< /Length " + str(len(content)).encode("latin-1") + b" >>\nstream\n" + content + b"\nendstream"
    )
    add_obj(5, stream_body)

    xref_offset = len(buf)
    buf.extend(b"xref\n0 6\n0000000000 65535 f \n")
    for off in offsets[1:]:
        buf.extend(f"{off:010d} 00000 n \n".encode("latin-1"))
    buf.extend(b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n")
    buf.extend(str(xref_offset).encode("latin-1"))
    buf.extend(b"\n%%EOF")
    return bytes(buf)


def make_docx_bytes() -> bytes:
    doc = Document()
    doc.add_paragraph("Hello paragraph")
    table = doc.add_table(rows=1, cols=1)
    table.rows[0].cells[0].text = "Cell text"
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_get_attachment_extracts_pdf_text_without_crashing(settings):
    payload = make_pdf_bytes()
    msg = fake_msg_with_attachments("1", [fake_attachment("invoice.pdf", payload, "application/pdf")])
    box = FakeBox([msg])
    out = mail.get_attachment(
        settings, "office@example.com", "1", "invoice.pdf", mailbox_factory=factory_for(box)
    )
    assert out["uid"] == "1"
    assert out["box"] == "office@example.com"
    assert out["filename"] == "invoice.pdf"
    assert out["mode"] == "text"
    assert isinstance(out["text"], str)
    assert out["truncated"] is False


def test_get_attachment_extracts_real_pdf_text_content(settings):
    payload = make_text_pdf_bytes("FAKTURA 12/2026 kwota 145,12")
    msg = fake_msg_with_attachments("1", [fake_attachment("invoice.pdf", payload, "application/pdf")])
    box = FakeBox([msg])
    out = mail.get_attachment(
        settings, "office@example.com", "1", "invoice.pdf", mailbox_factory=factory_for(box)
    )
    assert "FAKTURA 12/2026" in out["text"]


def test_get_attachment_never_marks_seen_and_fetches_single_uid(settings):
    payload = make_text_pdf_bytes("FAKTURA 12/2026 kwota 145,12")
    msg = fake_msg_with_attachments("1", [fake_attachment("invoice.pdf", payload, "application/pdf")])
    box = FakeBox([msg])
    mail.get_attachment(settings, "office@example.com", "1", "invoice.pdf", mailbox_factory=factory_for(box))
    assert box.calls[0]["mark_seen"] is False
    assert box.calls[0]["uid_list"] == ["1"]


def test_get_attachment_extracts_docx_text(settings):
    payload = make_docx_bytes()
    content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    msg = fake_msg_with_attachments("1", [fake_attachment("contract.docx", payload, content_type)])
    box = FakeBox([msg])
    out = mail.get_attachment(
        settings, "office@example.com", "1", "contract.docx", mailbox_factory=factory_for(box)
    )
    assert "Hello paragraph" in out["text"]
    assert "Cell text" in out["text"]


def test_get_attachment_decodes_plain_text(settings):
    payload = "Zażółć gęślą jaźń: a plain text document".encode()
    msg = fake_msg_with_attachments("1", [fake_attachment("note.txt", payload, "text/plain")])
    box = FakeBox([msg])
    out = mail.get_attachment(
        settings, "office@example.com", "1", "note.txt", mailbox_factory=factory_for(box)
    )
    assert out["text"] == "Zażółć gęślą jaźń: a plain text document"


def test_get_attachment_text_is_capped_at_60000_chars(settings):
    payload = ("a" * 70_000).encode("utf-8")
    msg = fake_msg_with_attachments("1", [fake_attachment("big.txt", payload, "text/plain")])
    box = FakeBox([msg])
    out = mail.get_attachment(
        settings, "office@example.com", "1", "big.txt", mailbox_factory=factory_for(box)
    )
    assert len(out["text"]) == 60_000
    assert out["truncated"] is True


def test_get_attachment_unknown_type_in_text_mode_is_source_error(settings):
    msg = fake_msg_with_attachments("1", [fake_attachment("photo.png", b"\x89PNG...", "image/png")])
    box = FakeBox([msg])
    with pytest.raises(SourceError, match="mode=base64"):
        mail.get_attachment(
            settings, "office@example.com", "1", "photo.png", mailbox_factory=factory_for(box)
        )


def test_get_attachment_base64_roundtrips_bytes(settings):
    import base64

    payload = b"\x89PNG\r\n\x1a\nbinarne dane obrazka"
    msg = fake_msg_with_attachments("1", [fake_attachment("photo.png", payload, "image/png")])
    box = FakeBox([msg])
    out = mail.get_attachment(
        settings, "office@example.com", "1", "photo.png", mode="base64", mailbox_factory=factory_for(box)
    )
    assert out["mode"] == "base64"
    assert base64.b64decode(out["data"]) == payload
    assert out["truncated"] is False


def test_get_attachment_base64_too_large_is_source_error(settings):
    payload = b"x" * (2 * 1024 * 1024 + 1)
    msg = fake_msg_with_attachments("1", [fake_attachment("big.bin", payload, "application/octet-stream")])
    box = FakeBox([msg])
    with pytest.raises(SourceError, match="too large"):
        mail.get_attachment(
            settings, "office@example.com", "1", "big.bin", mode="base64", mailbox_factory=factory_for(box)
        )


def test_get_attachment_missing_filename_lists_available(settings):
    msg = fake_msg_with_attachments("1", [fake_attachment("invoice.pdf", b"%PDF-1.4", "application/pdf")])
    box = FakeBox([msg])
    with pytest.raises(SourceError, match="invoice.pdf"):
        mail.get_attachment(
            settings, "office@example.com", "1", "missing.pdf", mailbox_factory=factory_for(box)
        )


def test_get_attachment_case_insensitive_filename_fallback(settings):
    msg = fake_msg_with_attachments(
        "1", [fake_attachment("Invoice.PDF", make_pdf_bytes(), "application/pdf")]
    )
    box = FakeBox([msg])
    out = mail.get_attachment(
        settings, "office@example.com", "1", "invoice.pdf", mailbox_factory=factory_for(box)
    )
    assert out["filename"] == "Invoice.PDF"


def test_get_attachment_unknown_mode_is_source_error(settings):
    msg = fake_msg_with_attachments("1", [fake_attachment("a.txt", b"x", "text/plain")])
    box = FakeBox([msg])
    with pytest.raises(SourceError, match="mode"):
        mail.get_attachment(
            settings, "office@example.com", "1", "a.txt", mode="pdf", mailbox_factory=factory_for(box)
        )


def test_get_attachment_missing_message_is_source_error(settings):
    box = FakeBox([])
    with pytest.raises(SourceError, match="not found"):
        mail.get_attachment(settings, "office@example.com", "99", "a.txt", mailbox_factory=factory_for(box))
