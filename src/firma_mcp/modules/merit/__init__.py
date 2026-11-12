"""Merit Aktiva / 360 Księgowość module: booked invoices and payments. Read-only.

Status: production for the Polish endpoint (``MERIT_COUNTRY=pl``), where this code runs
daily. The Estonian and Finnish endpoints are supported by the underlying library but have
not been tested with this server.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from fastmcp import FastMCP

from firma_mcp.config import read_group
from firma_mcp.errors import ConfigError, clamp_days
from firma_mcp.modules import PRODUCTION, Module
from firma_mcp.modules.merit.client import COUNTRIES, MAX_PDF_B64_CHARS, MeritReader, make_client
from firma_mcp.toolkit import READ_ONLY, Context, listing


@dataclass(frozen=True)
class MeritConfig:
    api_id: str
    api_key: str
    country: str


def load(env: Mapping[str, str]) -> MeritConfig | None:
    values = read_group(env, "merit", ("MERIT_API_ID", "MERIT_API_KEY"))
    if values is None:
        return None
    country = (env.get("MERIT_COUNTRY") or "pl").lower()
    if country not in COUNTRIES:
        raise ConfigError(f"MERIT_COUNTRY must be one of: {', '.join(COUNTRIES)}")
    return MeritConfig(api_id=values["MERIT_API_ID"], api_key=values["MERIT_API_KEY"], country=country)


def register(mcp: FastMCP, config: MeritConfig, ctx: Context) -> None:
    raw_client = ctx.overrides.get("merit") or make_client(config.api_id, config.api_key, config.country)
    reader = MeritReader(raw_client)
    ctx.services["merit"] = reader

    @mcp.tool(
        description="Purchase invoices booked in Merit Aktiva in the last `days` days (1..31, default 7). "
        "unpaid_only=true returns only the unpaid ones.",
        annotations=READ_ONLY,
    )
    def merit_purchases(days: int = 7, unpaid_only: bool = False) -> dict:
        d = clamp_days(days, default=7)
        return ctx.run("merit_purchases", lambda: listing(reader.purchases(d, unpaid_only)))

    @mcp.tool(
        description="Sales invoices issued in Merit Aktiva in the last `days` days (1..31, default 7). "
        "unpaid_only=true returns only the unpaid ones.",
        annotations=READ_ONLY,
    )
    def merit_sales(days: int = 7, unpaid_only: bool = False) -> dict:
        d = clamp_days(days, default=7)
        return ctx.run("merit_sales", lambda: listing(reader.sales(d, unpaid_only)))

    @mcp.tool(
        description="Incoming and outgoing payments recorded in Merit Aktiva in the last `days` days "
        "(1..31, default 31): date, bank, counterparty, amount, document number. `direction` is the "
        "raw direction code from Merit (a number the API does not document); `direction_label` is "
        'its reading inferred from live data: 1="wpływ" (incoming), 2="wypływ" (outgoing), '
        '0="kompensata" (set-off), any other code="nieznany" (unknown). Treat direction_label as a '
        "best guess, not a certainty.",
        annotations=READ_ONLY,
    )
    def merit_payments(days: int = 31) -> dict:
        d = clamp_days(days, default=31)
        return ctx.run("merit_payments", lambda: listing(reader.payments(d)))

    @mcp.tool(
        description="Checks in Merit Aktiva whether a payment of the given amount has already happened "
        "(tolerance 0.01), looking `days` days back (1..45, default 45). `amount` is a string with a "
        "decimal point, e.g. '123.45' (same format as total/gross from other tools, not a comma). "
        "Use it BEFORE reporting something as 'to pay': if found=true, it is already settled. "
        "`counterparty` (optional) narrows by counterparty name (partial, case-insensitive). `nip` is "
        "accepted for convenience, but Merit keeps no tax id on payments, so it does not affect matching.",
        annotations=READ_ONLY,
    )
    def payment_check(
        amount: str, counterparty: str | None = None, nip: str | None = None, days: int = 45
    ) -> dict:
        d = clamp_days(days, default=45, max_days=45)
        return ctx.run("payment_check", lambda: reader.find_payment(amount, counterparty, nip, d))

    @mcp.tool(
        description="Details of one Merit invoice: kind='purchase' (id from merit_purchases) or 'sales' "
        "(id from merit_sales). with_pdf=true adds the PDF as base64, truncated to "
        f"{MAX_PDF_B64_CHARS} characters (Attachment.truncated tells whether it was cut).",
        annotations=READ_ONLY,
    )
    def merit_invoice(kind: str, id: str, with_pdf: bool = False) -> dict:
        return ctx.run("merit_invoice", lambda: reader.invoice(kind, id, with_pdf))


MODULE = Module(
    name="merit",
    status=PRODUCTION,
    summary=(
        "Merit Aktiva: booked purchase and sales invoices and payments (merit_purchases, merit_sales, "
        "merit_invoice, merit_payments). Before reporting anything as 'to pay', call payment_check: "
        "if found=true it is already settled."
    ),
    load=load,
    register=register,
)
