"""Fakturownia tests run only against stubbed HTTP responses.

The stubs follow the public documentation (https://app.fakturownia.pl/api,
https://github.com/fakturownia/API). Nothing here has been compared with a live account, so
passing tests show that the code matches the documentation, not that it matches reality.
"""

import base64
import datetime as dt
import re
from pathlib import Path

import httpx
import pytest

from firma_mcp.errors import ConfigError, SourceError
from firma_mcp.modules import fakturownia as module
from firma_mcp.modules.fakturownia import client as fk

TOKEN = "stub-token"

SALE = {
    "id": 101,
    "number": "FS 1/11/2026",
    "kind": "vat",
    "income": "1",
    "issue_date": "2026-11-02",
    "sell_date": "2026-11-02",
    "payment_to": "2026-11-16",
    "paid_date": "",
    "status": "issued",
    "price_net": "1000,00",
    "price_tax": "230,00",
    "price_gross": "1230,00",
    "paid": "0,00",
    "currency": "PLN",
    "seller_name": "Our Company Ltd",
    "seller_tax_no": "1111111111",
    "buyer_name": "Customer One",
    "buyer_tax_no": "4444444444",
    "positions": [{"name": "Service A", "quantity": "1", "tax": "23", "total_price_gross": "1230,00"}],
}
# For a cost invoice Fakturownia swaps the roles: seller_* is OUR company, buyer_* the supplier.
COST = {
    **SALE,
    "id": 202,
    "number": "FK 7/2026",
    "income": "0",
    "status": "paid",
    "paid": "1230,00",
    "paid_date": "2026-11-05",
    "seller_name": "Our Company Ltd",
    "seller_tax_no": "1111111111",
    "buyer_name": "Supplier Two",
    "buyer_tax_no": "5555555555",
}


class Stub:
    """Records requests and answers like the documented API."""

    def __init__(self, invoices=None, status=200):
        self.requests: list[httpx.Request] = []
        self.invoices = invoices if invoices is not None else [SALE]
        self.status = status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"code": "error", "message": "unauthorized"})
        if self.status != 200:
            return httpx.Response(self.status, text="upstream failure")
        path = request.url.path
        if path == "/invoices.json":
            page = int(request.url.params.get("page", "1"))
            per_page = int(request.url.params["per_page"])
            return httpx.Response(200, json=self.invoices[(page - 1) * per_page : page * per_page])
        match = re.fullmatch(r"/invoices/(\d+)\.(json|pdf)", path)
        if match:
            found = [inv for inv in (SALE, COST) if inv["id"] == int(match.group(1))]
            if not found:
                return httpx.Response(404, json={"code": "error", "message": "not found"})
            if match.group(2) == "pdf":
                return httpx.Response(
                    200, content=b"%PDF-1.4 stub", headers={"Content-Type": "application/pdf"}
                )
            return httpx.Response(200, json=found[0])
        return httpx.Response(404)


def make_client(stub, token=TOKEN):
    return fk.FakturowniaClient("acme", token, transport=httpx.MockTransport(stub))


# -- pure functions ----------------------------------------------------------------


@pytest.mark.parametrize("given", ["acme", "ACME", "acme.fakturownia.pl", "https://acme.fakturownia.pl/"])
def test_base_url_accepts_name_or_host(given):
    assert fk.base_url(given) == "https://acme.fakturownia.pl"


@pytest.mark.parametrize("given", ["", "evil.example/x", "a b", "acme.evil.example", "../etc"])
def test_base_url_rejects_anything_else(given):
    with pytest.raises(ValueError):
        fk.base_url(given)


def test_money_normalises_documented_formats():
    assert fk.money("59,00") == "59.00"
    assert fk.money(72.57) == "72.57"
    assert fk.money("1 230,5") == "1230.50"
    assert fk.money(None) is None and fk.money("") is None
    assert fk.money("n/a") == "n/a"


def test_map_sales_invoice():
    out = fk.map_invoice(SALE)
    assert out == {
        "id": 101,
        "number": "FS 1/11/2026",
        "kind": "vat",
        "direction": "sales",
        "counterparty": "Customer One",
        "counterparty_nip": "4444444444",
        "issue_date": "2026-11-02",
        "sell_date": "2026-11-02",
        "due_date": "2026-11-16",
        "paid_date": None,
        "net": "1000.00",
        "vat": "230.00",
        "gross": "1230.00",
        "paid_amount": "0.00",
        "remaining": "1230.00",
        "currency": "PLN",
        "status": "issued",
        "paid": False,
    }


def test_map_cost_invoice_takes_counterparty_from_buyer_fields():
    out = fk.map_invoice(COST)
    assert out["direction"] == "cost"
    assert out["counterparty"] == "Supplier Two" and out["counterparty_nip"] == "5555555555"
    assert out["paid"] is True and out["remaining"] == "0.00"


def test_payment_status_overdue_only_when_unpaid_and_past_due():
    unpaid = fk.map_invoice(SALE)
    assert fk.payment_status(unpaid, today=dt.date(2026, 11, 17))["overdue"] is True
    assert fk.payment_status(unpaid, today=dt.date(2026, 11, 16))["overdue"] is False
    assert fk.payment_status(fk.map_invoice(COST), today=dt.date(2027, 1, 1))["overdue"] is False


def test_payment_status_partial_payment():
    partial = fk.map_invoice({**SALE, "status": "partial", "paid": "230,00"})
    out = fk.payment_status(partial, today=dt.date(2026, 11, 3))
    assert out["paid"] is False and out["paid_amount"] == "230.00" and out["remaining"] == "1000.00"


# -- HTTP behaviour ----------------------------------------------------------------


def test_sales_sends_bearer_token_and_documented_period_parameters():
    stub = Stub()
    out = make_client(stub).sales(7)
    assert [inv["number"] for inv in out] == ["FS 1/11/2026"]
    request = stub.requests[0]
    assert request.method == "GET"
    assert request.url.host == "acme.fakturownia.pl" and request.url.path == "/invoices.json"
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    params = request.url.params
    assert params["period"] == "more" and params["per_page"] == "100" and params["page"] == "1"
    assert "income" not in params
    span = dt.date.fromisoformat(params["date_to"]) - dt.date.fromisoformat(params["date_from"])
    assert span.days == 7


def test_token_is_never_sent_in_the_url():
    stub = Stub()
    make_client(stub).sales(7)
    assert TOKEN not in str(stub.requests[0].url)


def test_costs_request_income_no():
    stub = Stub(invoices=[COST])
    out = make_client(stub).costs(7)
    assert stub.requests[0].url.params["income"] == "no"
    assert out[0]["direction"] == "cost"


def test_unpaid_only_filters_on_documented_status():
    stub = Stub(
        invoices=[SALE, {**SALE, "id": 102, "status": "paid"}, {**SALE, "id": 103, "status": "partial"}]
    )
    out = make_client(stub).sales(7, unpaid_only=True)
    assert [inv["id"] for inv in out] == [101, 103]


def test_listing_follows_pages_until_a_short_page():
    stub = Stub(invoices=[{**SALE, "id": i} for i in range(1, 151)])
    out = make_client(stub).sales(31)
    assert len(out) == 150
    assert [r.url.params["page"] for r in stub.requests] == ["1", "2"]


def test_listing_stops_at_the_page_limit(monkeypatch):
    monkeypatch.setattr(fk, "MAX_PAGES", 2)
    stub = Stub(invoices=[{**SALE, "id": i} for i in range(1, 301)])
    with pytest.raises(SourceError, match="page limit"):
        make_client(stub).sales(31)


def test_invoice_returns_summary_positions_and_raw_record():
    out = make_client(Stub()).invoice("101")
    assert out["number"] == "FS 1/11/2026"
    assert out["positions"][0]["name"] == "Service A"
    assert out["raw"] == SALE
    assert "pdf_base64" not in out


def test_invoice_with_pdf():
    stub = Stub()
    out = make_client(stub).invoice(101, with_pdf=True)
    assert base64.b64decode(out["pdf_base64"]) == b"%PDF-1.4 stub"
    assert stub.requests[-1].url.path == "/invoices/101.pdf"


def test_invoice_pdf_too_large(monkeypatch):
    monkeypatch.setattr(fk, "MAX_PDF_BYTES", 4)
    with pytest.raises(SourceError, match="too large"):
        make_client(Stub()).invoice(101, with_pdf=True)


def test_payment_status_of_one_invoice():
    out = make_client(Stub()).payment_status(202)
    assert out["status"] == "paid" and out["paid"] is True and out["paid_date"] == "2026-11-05"


@pytest.mark.parametrize("bad_id", ["101/../../account", "abc", "", "1 OR 1=1"])
def test_invoice_id_must_be_numeric(bad_id):
    stub = Stub()
    with pytest.raises(SourceError, match="must be a number"):
        make_client(stub).invoice(bad_id)
    assert stub.requests == []  # nothing was sent


def test_wrong_token_becomes_source_error_naming_the_variable():
    with pytest.raises(SourceError, match="FAKTUROWNIA_API_TOKEN"):
        make_client(Stub(), token="wrong").sales(7)


def test_unknown_invoice_is_source_error():
    with pytest.raises(SourceError, match="404"):
        make_client(Stub()).invoice(999)


def test_server_error_is_source_error():
    with pytest.raises(SourceError, match="HTTP 500"):
        make_client(Stub(status=500)).sales(7)


def test_network_error_is_source_error():
    def boom(request):
        raise httpx.ConnectError("connection refused")

    with pytest.raises(SourceError, match="connection to Fakturownia failed"):
        make_client(boom).sales(7)


def test_non_list_listing_is_source_error():
    def handler(request):
        return httpx.Response(200, json={"error": "unexpected"})

    with pytest.raises(SourceError, match="expected a list"):
        make_client(handler).sales(7)


def test_non_json_body_is_source_error():
    def handler(request):
        return httpx.Response(200, text="<html>maintenance</html>")

    with pytest.raises(SourceError, match="unexpected Fakturownia response"):
        make_client(handler).sales(7)


def test_client_only_issues_get_requests():
    source = Path(fk.__file__).read_text(encoding="utf-8")
    assert re.findall(r"\.(post|put|patch|delete|request)\(", source) == []


# -- configuration -----------------------------------------------------------------


def test_load_requires_both_variables():
    assert module.load({}) is None
    with pytest.raises(ConfigError, match="FAKTUROWNIA_API_TOKEN"):
        module.load({"FAKTUROWNIA_DOMAIN": "acme"})
    config = module.load({"FAKTUROWNIA_DOMAIN": "acme", "FAKTUROWNIA_API_TOKEN": "t"})
    assert (config.domain, config.api_token) == ("acme", "t")


def test_load_rejects_a_bad_domain():
    with pytest.raises(ConfigError, match="FAKTUROWNIA_DOMAIN"):
        module.load({"FAKTUROWNIA_DOMAIN": "evil.example/x", "FAKTUROWNIA_API_TOKEN": "t"})


def test_module_is_marked_experimental():
    assert module.MODULE.status == "experimental"
