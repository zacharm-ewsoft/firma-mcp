"""booking_suggestions: a tool that needs two systems at once.

It is its own module so that the dependency is explicit: it is enabled only when both KSeF
(invoices waiting to be booked) and Merit (how similar invoices were booked before) are.

Status: production.
"""

from __future__ import annotations

import logging
import threading
import time

from fastmcp import FastMCP

from firma_mcp import matching
from firma_mcp.errors import clamp_days
from firma_mcp.modules import PRODUCTION, Module
from firma_mcp.modules.merit import suggestions
from firma_mcp.toolkit import READ_ONLY, Context, listing

log = logging.getLogger("firma_mcp.booking")

WARM_RETRY_SECONDS = 10 * 60  # after a failed warm-up, retry sooner than the full cache cycle


def _warm_history(merit) -> None:
    """Background thread that builds and refreshes the booking history before its cache
    expires, so the first booking_suggestions call after a start does not wait for the whole
    build (on live data about 150 paced requests, roughly two minutes).

    Failures are logged by exception type only, never with data, and never stop the thread.
    """
    interval = suggestions.CACHE_TTL_SECONDS - 30 * 60
    while True:
        try:
            suggestions.history(merit)
        except Exception as exc:  # noqa: BLE001 - a background warm-up must not kill the process
            log.warning("history warm-up failed: %s", type(exc).__name__)
            time.sleep(WARM_RETRY_SECONDS)
        else:
            time.sleep(interval)


def register(mcp: FastMCP, config: object, ctx: Context) -> None:
    ksef = ctx.services["ksef"]
    merit = ctx.services["merit"]

    if ctx.background:
        threading.Thread(
            target=_warm_history, args=(merit,), daemon=True, name="firma-mcp-history-warm"
        ).start()

    @mcp.tool(
        description="Booking suggestions for KSeF invoices from the last `days` days (1..31, default 31) "
        "that are not booked in Merit yet. For each: `account` is the ledger account used most often "
        "for this seller (by NIP) in about 12 months of Merit history, `vat_scheme` the most common "
        'VAT rate/scheme, `confidence` the share of the top account among history lines (e.g. "3/3"), '
        "`basis` the number of past invoices from this seller, `note` a message when the seller is "
        "new (no history). `history_truncated`/`history_invoices` say whether the history covered all "
        f"purchase invoices or only the newest {suggestions.MAX_DETAIL_FETCHES}; `history_partial=true` "
        "means the history build was cut short by Merit's rate limit (429) and the suggestions rest "
        "on incomplete data.",
        annotations=READ_ONLY,
    )
    def booking_suggestions(days: int = 31) -> dict:
        d = clamp_days(days, default=31)

        def work() -> dict:
            invoices = ksef.received(d)
            booked = merit.purchases(d + 31, False)
            unbooked = [inv for inv in matching.mark_booked(invoices, booked) if not inv["booked_in_merit"]]
            history_result = suggestions.history(merit)
            result = listing(suggestions.suggest(unbooked, history_result.rows))
            result["history_truncated"] = history_result.truncated
            result["history_invoices"] = history_result.invoices_used
            result["history_partial"] = history_result.partial
            return result

        return ctx.run("booking_suggestions", work)


MODULE = Module(
    name="booking",
    status=PRODUCTION,
    summary="booking_suggestions: proposed ledger account and VAT scheme for KSeF invoices not yet booked.",
    register=register,
    requires=("ksef", "merit"),
)
