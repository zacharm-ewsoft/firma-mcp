"""Fakturownia module: sales and cost invoices and their payment state. Read-only.

Status: EXPERIMENTAL and off by default (see ``load``). Written from the public API
documentation and tested only against stubbed HTTP responses. It has never been run against
a live Fakturownia account. See the notes at the top of ``client.py`` for exactly what is
documented and what is assumed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from fastmcp import FastMCP

from firma_mcp.config import read_flag, read_group
from firma_mcp.errors import ConfigError, clamp_days
from firma_mcp.modules import EXPERIMENTAL, Module
from firma_mcp.modules.fakturownia.client import MAX_PDF_BYTES, FakturowniaClient, base_url
from firma_mcp.toolkit import READ_ONLY, Context, listing

ENABLE_VAR = "FAKTUROWNIA_ENABLED"
NOTE = "EXPERIMENTAL (not tested on a live Fakturownia account). "


@dataclass(frozen=True)
class FakturowniaConfig:
    domain: str
    api_token: str


def load(env: Mapping[str, str]) -> FakturowniaConfig | None:
    """Unlike the other modules, credentials alone do not enable this one.

    Because it is experimental, it stays off until ``FAKTUROWNIA_ENABLED=true`` is set as
    well, so nobody runs untested code by merely filling in the credentials.
    """
    if not read_flag(env, ENABLE_VAR, default=False):
        return None
    values = read_group(env, "fakturownia", ("FAKTUROWNIA_DOMAIN", "FAKTUROWNIA_API_TOKEN"))
    if values is None:
        raise ConfigError(
            f"{ENABLE_VAR} is set, but FAKTUROWNIA_DOMAIN and FAKTUROWNIA_API_TOKEN are missing"
        )
    try:
        base_url(values["FAKTUROWNIA_DOMAIN"])
    except ValueError as exc:
        raise ConfigError(f"FAKTUROWNIA_DOMAIN: {exc}") from None
    return FakturowniaConfig(domain=values["FAKTUROWNIA_DOMAIN"], api_token=values["FAKTUROWNIA_API_TOKEN"])


def register(mcp: FastMCP, config: FakturowniaConfig, ctx: Context) -> None:
    client = ctx.overrides.get("fakturownia") or FakturowniaClient(config.domain, config.api_token)
    ctx.services["fakturownia"] = client

    @mcp.tool(
        description=NOTE + "Sales invoices issued in Fakturownia in the last `days` days (1..31, "
        "default 7). unpaid_only=true returns only those not fully paid.",
        annotations=READ_ONLY,
    )
    def fakturownia_sales(days: int = 7, unpaid_only: bool = False) -> dict:
        d = clamp_days(days, default=7)
        return ctx.run("fakturownia_sales", lambda: listing(client.sales(d, unpaid_only)))

    @mcp.tool(
        description=NOTE + "Cost invoices (expenses) recorded in Fakturownia in the last `days` days "
        "(1..31, default 7). unpaid_only=true returns only those not fully paid.",
        annotations=READ_ONLY,
    )
    def fakturownia_purchases(days: int = 7, unpaid_only: bool = False) -> dict:
        d = clamp_days(days, default=7)
        return ctx.run("fakturownia_purchases", lambda: listing(client.costs(d, unpaid_only)))

    @mcp.tool(
        description=NOTE + "Details of one Fakturownia invoice by id (from fakturownia_sales or "
        "fakturownia_purchases): summary, positions and the unmodified API record under `raw`. "
        f"with_pdf=true adds the PDF as base64 (at most {MAX_PDF_BYTES // (1024 * 1024)} MB).",
        annotations=READ_ONLY,
    )
    def fakturownia_invoice(id: str, with_pdf: bool = False) -> dict:
        return ctx.run("fakturownia_invoice", lambda: client.invoice(id, with_pdf))

    @mcp.tool(
        description=NOTE + "Payment state of one Fakturownia invoice by id: status, amount paid, "
        "amount remaining, due date and whether it is overdue. Use it BEFORE reporting an invoice "
        "as 'to pay'.",
        annotations=READ_ONLY,
    )
    def fakturownia_payment_status(id: str) -> dict:
        return ctx.run("fakturownia_payment_status", lambda: client.payment_status(id))


MODULE = Module(
    name="fakturownia",
    status=EXPERIMENTAL,
    summary=(
        "Fakturownia (experimental, untested on a live account): fakturownia_sales, "
        "fakturownia_purchases, fakturownia_invoice, fakturownia_payment_status."
    ),
    load=load,
    register=register,
)
