"""Read access to the Fakturownia API.

EXPERIMENTAL: written from the public documentation and never run against a live account.

Documentation used (read in November 2026):
* https://app.fakturownia.pl/api
* https://github.com/fakturownia/API

What the documentation states, and this client relies on:
* ``Authorization: Bearer API_TOKEN`` authenticates a request.
* ``GET /invoices.json`` accepts ``period=more`` with ``date_from``/``date_to``, ``page``,
  ``per_page`` (maximum 100) and ``income=no`` for cost invoices (expenses).
* ``GET /invoices/{id}.json`` returns one invoice, ``GET /invoices/{id}.pdf`` its PDF.
* Invoice fields are documented for creating invoices (``number``, ``status``, ``paid``,
  ``payment_to`` and so on). The documentation shows no full example of a GET response, so
  the mapping below ASSUMES that responses use the same field names.

Only GET requests are made.
"""

from __future__ import annotations

import base64
import datetime as dt
import re
from decimal import Decimal, InvalidOperation

import httpx

from firma_mcp.errors import SourceError

SOURCE = "fakturownia"
PER_PAGE = 100  # documented maximum
MAX_PAGES = 20  # hard stop: at most 2000 invoices per call
MAX_PDF_BYTES = 2 * 1024 * 1024
TIMEOUT = 30
# Documented status values. Everything except "paid" still has money outstanding.
STATUSES = ("issued", "sent", "paid", "partial", "rejected")
DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def base_url(domain: str) -> str:
    """'mycompany' -> 'https://mycompany.fakturownia.pl'. Also accepts the full host name."""
    name = domain.strip().lower().removeprefix("https://").split("/")[0].removesuffix(".fakturownia.pl")
    if not DOMAIN_RE.match(name):
        raise ValueError(f"not a valid Fakturownia account name: {domain!r}")
    return f"https://{name}.fakturownia.pl"


def money(value) -> str | None:
    """Fakturownia documents amounts both as '59,00' and as 59.0; normalise to '59.00'."""
    if value is None or value == "":
        return None
    try:
        return f"{Decimal(str(value).replace(',', '.').replace(' ', '')).quantize(Decimal('0.01'))}"
    except InvalidOperation:
        return str(value)


def _remaining(gross: str | None, paid: str | None) -> str | None:
    try:
        return f"{Decimal(gross) - Decimal(paid or '0')}" if gross is not None else None
    except (InvalidOperation, TypeError):
        return None


def map_invoice(raw: dict) -> dict:
    """Common shape for a sales or a cost invoice.

    The other party is always in the ``buyer_*`` fields. The documentation says that on a
    cost invoice (``income`` = 0) the ``seller_*`` fields are printed in the "buyer" section
    and ``buyer_*`` in the "seller" section; in other words ``seller_*`` is always our own
    company and ``buyer_*`` the customer (sales) or the supplier (costs).
    """
    is_income = str(raw.get("income", "1")).lower() not in ("0", "false", "no")
    gross, paid = money(raw.get("price_gross")), money(raw.get("paid"))
    status = raw.get("status")
    return {
        "id": raw.get("id"),
        "number": raw.get("number"),
        "kind": raw.get("kind"),
        "direction": "sales" if is_income else "cost",
        "counterparty": raw.get("buyer_name"),
        "counterparty_nip": raw.get("buyer_tax_no"),
        "issue_date": raw.get("issue_date"),
        "sell_date": raw.get("sell_date"),
        "due_date": raw.get("payment_to"),
        "paid_date": raw.get("paid_date") or None,
        "net": money(raw.get("price_net")),
        "vat": money(raw.get("price_tax")),
        "gross": gross,
        "paid_amount": paid,
        "remaining": _remaining(gross, paid),
        "currency": raw.get("currency"),
        "status": status,
        "paid": status == "paid",
    }


def payment_status(invoice: dict, today: dt.date | None = None) -> dict:
    """Payment state of one mapped invoice, including whether it is overdue."""
    today = today or dt.date.today()
    overdue = False
    if not invoice["paid"] and invoice.get("due_date"):
        try:
            overdue = dt.date.fromisoformat(str(invoice["due_date"])[:10]) < today
        except ValueError:
            overdue = False
    keys = ("id", "number", "direction", "counterparty", "status", "paid", "gross", "paid_amount")
    return {
        **{key: invoice.get(key) for key in keys},
        "remaining": invoice.get("remaining"),
        "currency": invoice.get("currency"),
        "due_date": invoice.get("due_date"),
        "paid_date": invoice.get("paid_date"),
        "overdue": overdue,
    }


class FakturowniaClient:
    def __init__(self, domain: str, api_token: str, *, transport: httpx.BaseTransport | None = None):
        self._http = httpx.Client(
            base_url=base_url(domain),
            timeout=TIMEOUT,
            transport=transport,
            headers={"Authorization": f"Bearer {api_token}", "Accept": "application/json"},
        )

    def _get(self, path: str, what: str, **params) -> httpx.Response:
        try:
            response = self._http.get(path, params=params or None)
        except httpx.HTTPError as exc:
            raise SourceError(SOURCE, f"connection to Fakturownia failed: {exc}") from exc
        if response.status_code == 401:
            raise SourceError(SOURCE, f"{what}: HTTP 401, check FAKTUROWNIA_API_TOKEN")
        if response.status_code == 404:
            raise SourceError(SOURCE, f"{what}: not found (HTTP 404)")
        if response.status_code >= 400:
            raise SourceError(SOURCE, f"{what}: HTTP {response.status_code} {response.text[:300]}")
        return response

    def _json(self, response: httpx.Response, what: str):
        try:
            return response.json()
        except ValueError as exc:
            raise SourceError(SOURCE, f"unexpected Fakturownia response for {what}: {exc}") from exc

    def _list(
        self, days: int, *, income: bool, unpaid_only: bool, today: dt.date | None = None
    ) -> list[dict]:
        end = today or dt.date.today()
        params = {
            "period": "more",
            "date_from": (end - dt.timedelta(days=days)).isoformat(),
            "date_to": end.isoformat(),
            "per_page": PER_PAGE,
        }
        if not income:
            params["income"] = "no"
        what = "sales invoices" if income else "cost invoices"
        out: list[dict] = []
        for page in range(1, MAX_PAGES + 1):
            rows = self._json(self._get("/invoices.json", what, page=page, **params), what)
            if not isinstance(rows, list):
                raise SourceError(
                    SOURCE,
                    f"unexpected Fakturownia response for {what}: expected a list, got {type(rows).__name__}",
                )
            out.extend(map_invoice(r) for r in rows if isinstance(r, dict))
            if len(rows) < PER_PAGE:
                break
        else:
            raise SourceError(SOURCE, f"page limit ({MAX_PAGES}) exceeded while listing {what}")
        # The documentation names no query filter for unpaid invoices, so filter here.
        return [inv for inv in out if not inv["paid"]] if unpaid_only else out

    def sales(self, days: int, unpaid_only: bool = False) -> list[dict]:
        return self._list(days, income=True, unpaid_only=unpaid_only)

    def costs(self, days: int, unpaid_only: bool = False) -> list[dict]:
        return self._list(days, income=False, unpaid_only=unpaid_only)

    def invoice_raw(self, invoice_id: int | str) -> dict:
        invoice_id = _numeric_id(invoice_id)
        what = f"invoice {invoice_id}"
        data = self._json(self._get(f"/invoices/{invoice_id}.json", what), what)
        if not isinstance(data, dict):
            raise SourceError(SOURCE, f"unexpected Fakturownia response for {what}: expected an object")
        return data

    def invoice(self, invoice_id: int | str, with_pdf: bool = False) -> dict:
        """One invoice: the mapped summary, its positions, and the unmodified API record."""
        raw = self.invoice_raw(invoice_id)
        out = {**map_invoice(raw), "positions": raw.get("positions") or [], "raw": raw}
        if with_pdf:
            pdf = self._get(
                f"/invoices/{_numeric_id(invoice_id)}.pdf", f"PDF of invoice {invoice_id}"
            ).content
            if len(pdf) > MAX_PDF_BYTES:
                raise SourceError(SOURCE, f"PDF is too large ({len(pdf)} B > {MAX_PDF_BYTES} B)")
            out["pdf_base64"] = base64.b64encode(pdf).decode("ascii")
        return out

    def payment_status(self, invoice_id: int | str) -> dict:
        return payment_status(map_invoice(self.invoice_raw(invoice_id)))


def _numeric_id(invoice_id: int | str) -> str:
    """Invoice ids are numbers. Rejecting anything else keeps tool input out of the URL path."""
    text = str(invoice_id).strip()
    if not text.isdigit():
        raise SourceError(SOURCE, f"invoice id must be a number, got {invoice_id!r}")
    return text
