"""KSeF (Krajowy System e-Faktur) module: invoices received by the company. Read-only.

Status: production. This code runs daily against the production KSeF API.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

from fastmcp import FastMCP

from firma_mcp import matching
from firma_mcp.config import read_group
from firma_mcp.errors import ConfigError, SourceError, clamp_days
from firma_mcp.modules import PRODUCTION, Module
from firma_mcp.modules.ksef.client import BASE_URLS, KsefClient
from firma_mcp.toolkit import READ_ONLY, Context, listing

log = logging.getLogger("firma_mcp.ksef")

XML_MAX_CHARS = 50_000


@dataclass(frozen=True)
class KsefConfig:
    token: str
    nip: str
    env: str


def load(env: Mapping[str, str]) -> KsefConfig | None:
    values = read_group(env, "ksef", ("KSEF_TOKEN", "KSEF_NIP"))
    if values is None:
        return None
    ksef_env = env.get("KSEF_ENV") or "prod"
    if ksef_env not in BASE_URLS:
        raise ConfigError(f"KSEF_ENV must be one of: {', '.join(BASE_URLS)}")
    nip = "".join(ch for ch in values["KSEF_NIP"] if ch.isdigit())
    if len(nip) != 10:
        raise ConfigError("KSEF_NIP must be a 10-digit NIP")
    return KsefConfig(token=values["KSEF_TOKEN"], nip=nip, env=ksef_env)


def register(mcp: FastMCP, config: KsefConfig, ctx: Context) -> None:
    client = ctx.overrides.get("ksef") or KsefClient(config.token, config.nip, config.env)
    ctx.services["ksef"] = client
    merit = ctx.services.get("merit")  # present only when the Merit module is enabled

    received_doc = (
        "Invoices received in KSeF where the company is the buyer, from the last `days` days "
        "(1..31, default 7)."
    )
    if merit is not None:
        received_doc += " The `booked_in_merit` field says whether the invoice is already booked in Merit."

    @mcp.tool(description=received_doc, annotations=READ_ONLY)
    def ksef_received(days: int = 7) -> dict:
        d = clamp_days(days, default=7)

        def work() -> dict:
            invoices = client.received(d)
            if merit is None:
                return listing(invoices)
            try:
                # Look further back in Merit than in KSeF: an invoice is often booked with a
                # document date earlier than the day it reached KSeF.
                booked = merit.purchases(d + 31, False)
            except SourceError as exc:
                log.warning("ksef_received: Merit unavailable (%s), returning without flags", exc.source)
                booked = []
            return listing(matching.mark_booked(invoices, booked))

        return ctx.run("ksef_received", work)

    @mcp.tool(
        description="XML of one KSeF invoice by its KSeF number (from ksef_received). "
        f"Truncated to {XML_MAX_CHARS} characters.",
        annotations=READ_ONLY,
    )
    def ksef_invoice(ksef_number: str) -> dict:
        def work() -> dict:
            xml = client.invoice_xml(ksef_number)
            return {
                "ksef_number": ksef_number,
                "xml": xml[:XML_MAX_CHARS],
                "truncated": len(xml) > XML_MAX_CHARS,
            }

        return ctx.run("ksef_invoice", work)


MODULE = Module(
    name="ksef",
    status=PRODUCTION,
    summary="KSeF: invoices received by the company (ksef_received, then ksef_invoice for the XML).",
    load=load,
    register=register,
)
