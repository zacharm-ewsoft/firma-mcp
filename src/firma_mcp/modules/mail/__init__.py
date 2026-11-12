"""Mail module: read the INBOX of one or more mailboxes over IMAP. Read-only.

Status: production, with one caveat. This code runs daily against a single IMAP server
(IMAP over TLS on port 993, login = e-mail address, an app password per mailbox). Other
servers that follow the same setup should work but have not been tested.
"""

from __future__ import annotations

from collections.abc import Mapping

from fastmcp import FastMCP

from firma_mcp.config import split_csv
from firma_mcp.errors import ConfigError, clamp_days
from firma_mcp.modules import PRODUCTION, Module
from firma_mcp.modules.mail import imap
from firma_mcp.modules.mail.imap import MailConfig
from firma_mcp.toolkit import READ_ONLY, Context, listing

DEFAULT_PORT = 993


def env_key_for_box(box: str) -> str:
    """office@example.com -> MAIL_PASSWORD__office_example_com"""
    safe = box.replace("@", "_").replace(".", "_").replace("-", "_")
    return f"MAIL_PASSWORD__{safe}"


def load(env: Mapping[str, str]) -> MailConfig | None:
    host, boxes = env.get("MAIL_HOST"), split_csv(env.get("MAIL_BOXES"))
    if not host and not boxes:
        return None
    if not host or not boxes:
        raise ConfigError("Module 'mail' is partly configured; set both MAIL_HOST and MAIL_BOXES")
    passwords: dict[str, str] = {}
    for box in boxes:
        key = env_key_for_box(box)
        if not env.get(key):
            raise ConfigError(f"No password for mailbox {box}: set {key}")
        passwords[box] = env[key]
    try:
        port = int(env.get("MAIL_PORT") or DEFAULT_PORT)
    except ValueError:
        raise ConfigError("MAIL_PORT must be a number") from None
    return MailConfig(host=host, port=port, boxes=boxes, passwords=passwords)


def register(mcp: FastMCP, config: MailConfig, ctx: Context) -> None:
    mailbox_factory = ctx.overrides.get("mail") or imap.open_box
    boxes = ", ".join(config.boxes)

    @mcp.tool(
        description=f"Messages in the INBOX of a mailbox ({boxes}) from the last `days` days (1..31, "
        "default 3). Returns sender, subject, date, a 200-character preview and attachment names. "
        f"The 40 newest messages by default, at most {imap.MAX_LIST_LIMIT} (`limit`). Does not mark "
        "anything as read. Use mail_get for the full text. When the mailbox is busy "
        "(truncated=true), lower `days` or `limit`.",
        annotations=READ_ONLY,
    )
    def mail_list(box: str, days: int = 3, limit: int = 40) -> dict:
        d = clamp_days(days, default=3)
        effective_limit = max(1, min(limit, imap.MAX_LIST_LIMIT))
        result = ctx.run(
            "mail_list",
            lambda: listing(imap.list_messages(config, box, d, mailbox_factory=mailbox_factory, limit=limit)),
        )
        if "items" in result:
            result["truncated"] = len(result["items"]) >= effective_limit
        return result

    @mcp.tool(
        description="Full text of one message (uid from mail_list) in the given mailbox. "
        "Attachment contents are not included.",
        annotations=READ_ONLY,
    )
    def mail_get(box: str, uid: str) -> dict:
        return ctx.run(
            "mail_get", lambda: imap.get_message(config, box, uid, mailbox_factory=mailbox_factory)
        )

    @mcp.tool(
        description="Content of one attachment (uid from mail_list, file name from mail_list/mail_get). "
        "Use it when the matter depends on the document itself (an invoice, a contract), not just on "
        "the message. mode='text' (default): text of a PDF/DOCX/TXT, truncated to "
        f"{imap.ATTACHMENT_TEXT_MAX_CHARS} characters (see `truncated`). mode='base64': raw bytes "
        f"as base64, at most {imap.ATTACHMENT_BASE64_MAX_BYTES // (1024 * 1024)} MB, for images and "
        "types without text extraction.",
        annotations=READ_ONLY,
    )
    def mail_attachment(box: str, uid: str, filename: str, mode: str = "text") -> dict:
        return ctx.run(
            "mail_attachment",
            lambda: imap.get_attachment(config, box, uid, filename, mode, mailbox_factory=mailbox_factory),
        )


MODULE = Module(
    name="mail",
    status=PRODUCTION,
    summary="Mail: mail_list for an overview, then mail_get and mail_attachment for single items.",
    load=load,
    register=register,
)
