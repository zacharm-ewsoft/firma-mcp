"""Booking suggestions for KSeF invoices that are not booked in Merit yet.

For each such invoice we propose the ledger account and VAT scheme that the same seller
(matched by NIP) received most often in Merit over roughly the last 12 months.

Building that history is expensive: one list call per 89-day window (the API's limit) plus
one detail call per invoice to get its lines. The result is therefore cached in process
memory for ``CACHE_TTL_SECONDS``.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

from firma_mcp import matching
from firma_mcp.errors import SourceError

WINDOW_DAYS = 89  # Merit rejects longer periods for purchase invoices
HISTORY_MONTHS_DEFAULT = 12
HISTORY_APPROX_DAYS_PER_MONTH = 30
CACHE_TTL_SECONDS = 6 * 60 * 60
NO_HISTORY_NOTE = "nowy kontrahent — brak historii"  # Polish on purpose: clients depend on it
# Hard cap on detail calls per history build. Without it an account with hundreds of purchase
# invoices a year would make one tool call take minutes on a cold cache. We keep the newest
# invoices, which are the most relevant for fresh KSeF invoices.
MAX_DETAIL_FETCHES = 150
# Merit documents a limit of 100 requests per minute
# (https://api.merit.ee/connecting-robots/reference-manual/rate-limiter/). A 0.8 s pause is
# about 75 per minute and leaves room for other Merit calls made by the same process.
DETAIL_FETCH_INTERVAL = 0.8
# On HTTP 429 wait these many seconds in turn before retrying (six attempts in total).
RATE_LIMIT_BACKOFFS = (2, 4, 8, 16, 32)


@dataclass(frozen=True)
class HistoryRow:
    invoice_id: str
    vendor_nip: str
    vendor_name: str | None
    account_code: str | None
    tax_name: str | None


@dataclass(frozen=True)
class HistoryResult:
    rows: list[HistoryRow]
    # True when the history is incomplete: more invoices than MAX_DETAIL_FETCHES, or `partial`.
    truncated: bool
    invoices_used: int  # invoices whose lines were actually fetched
    # True when a persistent 429 stopped the build after some rows had been collected.
    # A partial history is more useful than no suggestions at all.
    partial: bool = False


# In-process cache keyed by id(reader). Tests must call clear_cache() between cases, because
# id() values are reused once an object has been garbage collected.
_cache: dict[int, tuple[float, HistoryResult]] = {}
# One global lock, held for the whole read-and-build, so the warm-up thread and a tool call
# can never build the same expensive history twice in parallel.
_cache_lock = threading.Lock()


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _is_rate_limited(exc: SourceError) -> bool:
    text = str(exc)
    return "429" in text or "too many requests" in text.lower()


def _call_with_backoff(fn: Callable[[], object], *, sleep: Callable[[float], None]):
    """Call ``fn``; on a rate-limit error wait and retry. Other errors propagate at once.
    When every attempt is rate limited the last error is raised and the caller decides."""
    last_exc: SourceError | None = None
    for backoff in (None, *RATE_LIMIT_BACKOFFS):
        if backoff is not None:
            sleep(backoff)
        try:
            return fn()
        except SourceError as exc:
            if not _is_rate_limited(exc):
                raise
            last_exc = exc
    assert last_exc is not None
    raise last_exc


def _windows(today: dt.date, total_days: int) -> list[tuple[str, str]]:
    """Windows going back from ``today``, each at most WINDOW_DAYS long.

    Neighbouring windows share their boundary day, so Merit may return that day's invoices
    twice. That is harmless: ``history`` de-duplicates by invoice id before fetching details.
    """
    end = today
    earliest = today - dt.timedelta(days=total_days)
    windows: list[tuple[str, str]] = []
    while end > earliest:
        start = max(earliest, end - dt.timedelta(days=WINDOW_DAYS))
        windows.append((start.strftime("%Y%m%d"), end.strftime("%Y%m%d")))
        end = start
    return windows


def history(
    reader,
    months: int = HISTORY_MONTHS_DEFAULT,
    *,
    today: dt.date | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> HistoryResult:
    """Purchase-invoice lines from the last ``months`` months, one ``HistoryRow`` per line.

    Detail calls are paced by ``DETAIL_FETCH_INTERVAL`` and retried on 429. If the rate limit
    persists while fetching details, we stop and return what we have (``partial=True``) as
    long as at least one row was collected; otherwise the error propagates.
    """
    cache_key = id(reader)
    with _cache_lock:
        now = time.monotonic()
        cached = _cache.get(cache_key)
        if cached is not None and now - cached[0] < CACHE_TTL_SECONDS:
            return cached[1]

        today = today or dt.date.today()
        total_days = months * HISTORY_APPROX_DAYS_PER_MONTH
        unique_invoices: dict[str, dict] = {}
        for start, end in _windows(today, total_days):
            page = _call_with_backoff(
                lambda s=start, e=end: reader.purchases_between(s, e, False), sleep=sleep
            )
            for inv in page:
                invoice_id = inv.get("id")
                if invoice_id and invoice_id not in unique_invoices:
                    unique_invoices[invoice_id] = inv

        ordered = sorted(
            unique_invoices.values(), key=lambda inv: inv.get("document_date") or "", reverse=True
        )
        over_cap = len(ordered) > MAX_DETAIL_FETCHES
        selected = ordered[:MAX_DETAIL_FETCHES]

        rows: list[HistoryRow] = []
        processed = 0
        partial = False
        for i, inv in enumerate(selected):
            try:
                detail = _call_with_backoff(lambda inv=inv: reader.purchase_detail(inv["id"]), sleep=sleep)
            except SourceError:
                if not rows:
                    raise  # nothing collected, so there is no meaningful partial result
                partial = True
                break

            processed += 1
            for line in detail.get("Lines") or []:
                if not isinstance(line, dict):
                    continue
                rows.append(
                    HistoryRow(
                        invoice_id=inv["id"],
                        vendor_nip=inv.get("vendor_nip") or "",
                        vendor_name=inv.get("vendor"),
                        account_code=line.get("AccountCode"),
                        tax_name=line.get("TaxName"),
                    )
                )
            if i < len(selected) - 1:
                sleep(DETAIL_FETCH_INTERVAL)

        result = HistoryResult(
            rows=rows, truncated=over_cap or partial, invoices_used=processed, partial=partial
        )
        _cache[cache_key] = (time.monotonic(), result)
        return result


def _suggest_one(inv: dict, rows: list[HistoryRow]) -> dict:
    base = {
        "ksef_number": inv.get("ksef_number"),
        "invoice_number": inv.get("invoice_number"),
        "seller_nip": inv.get("seller_nip"),
        "seller_name": inv.get("seller_name"),
        "gross": inv.get("gross"),
        "currency": inv.get("currency"),
        "ksef_date": inv.get("ksef_date"),
    }
    if not rows:
        return {
            **base,
            "account": None,
            "vat_scheme": None,
            "confidence": None,
            "basis": 0,
            "note": NO_HISTORY_NOTE,
        }

    # `confidence` counts history LINES (one invoice can carry several accounts, and lines are
    # what vote for an account). `basis` counts distinct source INVOICES, which is the easier
    # number for a person to read as "how much evidence is this based on".
    total_lines = len(rows)
    basis = len({r.invoice_id for r in rows})
    account_counts = Counter(r.account_code for r in rows if r.account_code)
    tax_counts = Counter(r.tax_name for r in rows if r.tax_name)
    account, confidence = None, None
    if account_counts:
        account, top_count = account_counts.most_common(1)[0]
        confidence = f"{top_count}/{total_lines}"
    vat_scheme = tax_counts.most_common(1)[0][0] if tax_counts else None
    return {
        **base,
        "account": account,
        "vat_scheme": vat_scheme,
        "confidence": confidence,
        "basis": basis,
        "note": None,
    }


def suggest(ksef_invoices: list[dict], history_rows: list[HistoryRow]) -> list[dict]:
    """One suggestion per KSeF invoice, drawn from the history of the same seller NIP.
    Expects ``ksef_invoices`` already filtered down to those not booked in Merit."""
    by_nip: dict[str, list[HistoryRow]] = {}
    for row in history_rows:
        nip = matching.normalize_nip(row.vendor_nip)
        if nip:
            by_nip.setdefault(nip, []).append(row)

    return [
        _suggest_one(inv, by_nip.get(matching.normalize_nip(inv.get("seller_nip")), []))
        for inv in ksef_invoices
    ]
