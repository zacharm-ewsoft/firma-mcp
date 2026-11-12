"""Read access to Merit Aktiva (sold in Poland as 360 Księgowość).

``MeritReader`` is a deliberately narrow facade over the ``merit-activa-api`` client. That
library can also create and delete documents; the facade exposes only reads, and a test
asserts that this file never references a write method.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal, InvalidOperation

from merit_activa import MeritClient

from firma_mcp.errors import SourceError

SOURCE = "merit"
TIMEOUT = 30
MAX_PDF_B64_CHARS = 200_000
COUNTRIES = ("pl", "ee", "fi")

# `Direction` is a numeric code that the API documentation does not explain. The labels were
# inferred from live data: 1 on a payment received from a customer, 2 on a payment we made,
# 0 on a set-off. Any other code is reported as unknown rather than guessed. The label values
# are in Polish because clients already depend on them.
PAYMENT_DIRECTION_LABELS = {1: "wpływ", 2: "wypływ", 0: "kompensata"}
UNKNOWN_DIRECTION = "nieznany"


def make_client(api_id: str, api_key: str, country: str = "pl") -> MeritClient:
    return MeritClient(api_id=api_id, api_key=api_key, country=country, timeout=TIMEOUT)


def period(days: int, today: dt.date | None = None) -> tuple[str, str]:
    end = today or dt.date.today()
    start = end - dt.timedelta(days=days)
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


def money(value) -> str | None:
    if value is None:
        return None
    try:
        return f"{Decimal(str(value)).quantize(Decimal('0.01'))}"
    except InvalidOperation:
        return str(value)


def _date(value) -> str | None:
    if not value:
        return None
    return str(value)[:10]


def map_purchase(raw: dict) -> dict:
    # Verified on live data: `VendorRegNo` holds the REGON/KRS number and the NIP is in
    # `VatRegNo`. Reading them the other way round made every NIP comparison miss.
    # `VatRegNo` can be empty (e.g. a private person), hence the fallback.
    return {
        "id": raw.get("PIHId"),
        "number": raw.get("BillNo"),
        "vendor": raw.get("VendorName"),
        "vendor_nip": raw.get("VatRegNo") or raw.get("VendorRegNo"),
        "vendor_regon": raw.get("VendorRegNo"),
        "document_date": _date(raw.get("DocumentDate")),
        "due_date": _date(raw.get("DueDate")),
        "total": money(raw.get("TotalSum")),
        "paid_amount": money(raw.get("PaidAmount")),
        "paid": bool(raw.get("Paid")),
        "currency": raw.get("CurrencyCode"),
        "has_pdf": bool(raw.get("FileExists")),
    }


def map_sale(raw: dict) -> dict:
    # Same convention as map_purchase: `RegNo` is REGON/KRS, the NIP is in `VatRegNo`.
    return {
        "id": raw.get("SIHId"),
        "number": raw.get("InvoiceNo"),
        "customer": raw.get("CustomerName"),
        "customer_nip": raw.get("VatRegNo") or raw.get("RegNo"),
        "customer_regon": raw.get("RegNo"),
        "document_date": _date(raw.get("DocumentDate")),
        "due_date": _date(raw.get("DueDate")),
        "total": money(raw.get("TotalSum")),
        "paid_amount": money(raw.get("PaidAmount")),
        "paid": bool(raw.get("Paid")),
        "currency": raw.get("CurrencyCode"),
    }


def map_payment(raw: dict) -> dict:
    direction = raw.get("Direction")
    return {
        "id": raw.get("PHId"),
        "date": _date(raw.get("DocumentDate")),
        "bank": raw.get("BankName"),
        "counterparty": raw.get("CounterPartName"),
        "counterparty_type": raw.get("CounterPartType"),
        "direction": direction,
        "direction_label": PAYMENT_DIRECTION_LABELS.get(direction, UNKNOWN_DIRECTION),
        "amount": money(raw.get("Amount")),
        "currency": raw.get("CurrencyCode"),
        "document_no": raw.get("DocumentNo"),
    }


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - every API or network failure is a source error
        raise SourceError(SOURCE, f"Merit API ({type(exc).__name__}): {exc}") from exc


def _expect_list(rows, what: str) -> list:
    """Merit sometimes answers with plain text (such as 'OK') instead of JSON. Catch it here,
    before mapping."""
    if rows is None:
        return []
    if not isinstance(rows, list):
        preview = str(rows)[:100]
        raise SourceError(
            SOURCE,
            f"Unexpected Merit response for {what}: expected a list, got {type(rows).__name__} ({preview!r})",
        )
    return [r for r in rows if isinstance(r, dict)]


def _expect_dict(data, what: str) -> dict:
    if data is None:
        return {}
    if not isinstance(data, dict):
        preview = str(data)[:100]
        raise SourceError(
            SOURCE,
            f"Unexpected Merit response for {what}: expected an object, "
            f"got {type(data).__name__} ({preview!r})",
        )
    return data


def _normalize_name(value: str) -> str:
    return re.sub(r"[^\w]+", "", value, flags=re.UNICODE).casefold()


class MeritReader:
    """The only way the rest of the server talks to Merit."""

    def __init__(self, client: MeritClient):
        self._client = client

    def purchases_between(self, start: str, end: str, unpaid_only: bool) -> list[dict]:
        rows = _call(self._client.get_purchase_invoices, start, end, unpaid_only)
        return [map_purchase(r) for r in _expect_list(rows, "purchase invoices")]

    def purchases(self, days: int, unpaid_only: bool) -> list[dict]:
        start, end = period(days)
        return self.purchases_between(start, end, unpaid_only)

    def sales(self, days: int, unpaid_only: bool) -> list[dict]:
        start, end = period(days)
        rows = _call(self._client.get_invoices, start, end, unpaid_only)
        return [map_sale(r) for r in _expect_list(rows, "sales invoices")]

    def payments(self, days: int) -> list[dict]:
        start, end = period(days)
        rows = _call(self._client.get_payments, start, end)
        return [map_payment(r) for r in _expect_list(rows, "payments")]

    def purchase_detail(self, invoice_id: str) -> dict:
        """One purchase invoice with its lines and without the attachment. Feeds the booking
        history, which always wants lines and never the PDF."""
        data = _call(
            self._client._request, "getpurchorder", {"Id": invoice_id, "SkipAttachment": True}, version="v2"
        )
        return _expect_dict(data, f"purchase invoice {invoice_id}")

    def invoice(self, kind: str, invoice_id: str, with_pdf: bool = False) -> dict:
        # The library's get_purchase_invoice_details/get_invoice_details send the id under a
        # wrong parameter name and the API answers 400 "Id is required." Calling the request
        # method directly with "Id" works around that library bug.
        if kind == "purchase":
            data = _call(
                self._client._request,
                "getpurchorder",
                {"Id": invoice_id, "SkipAttachment": not with_pdf},
                version="v2",
            )
        elif kind == "sales":
            data = _call(self._client._request, "getinvoice", {"Id": invoice_id}, version="v2")
        else:
            raise SourceError(SOURCE, "kind must be 'purchase' or 'sales'")
        data = dict(_expect_dict(data, "invoice details"))
        if not with_pdf:
            data.pop("Attachment", None)
            return data
        attachment = data.get("Attachment")
        if isinstance(attachment, dict):
            attachment = dict(attachment)
            content = attachment.get("FileContent")
            truncated = isinstance(content, str) and len(content) > MAX_PDF_B64_CHARS
            if truncated:
                attachment["FileContent"] = content[:MAX_PDF_B64_CHARS]
            attachment["truncated"] = truncated
            data["Attachment"] = attachment
        return data

    def find_payment(
        self, amount: str, counterparty: str | None = None, nip: str | None = None, days: int = 45
    ) -> dict:
        """Look for a payment matching the amount (within 0.01) and optionally the counterparty.

        ``nip`` is accepted for the caller's convenience, but Merit does not expose a tax id on
        payments, so it takes no part in matching.
        """
        try:
            target = Decimal(str(amount))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise SourceError(SOURCE, f"invalid amount to check: {amount!r}") from exc

        wanted_name = _normalize_name(counterparty) if counterparty else None
        matches = []
        for row in self.payments(days):
            row_amount = row.get("amount")
            if row_amount is None:
                continue
            try:
                if abs(Decimal(row_amount) - target) > Decimal("0.01"):
                    continue
            except InvalidOperation:
                continue
            if wanted_name is not None and wanted_name not in _normalize_name(row.get("counterparty") or ""):
                continue
            matches.append(row)
        return {"found": bool(matches), "matches": matches, "checked_days": days}
