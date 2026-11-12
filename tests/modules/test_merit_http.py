"""Merit tests at the HTTP level: the real merit-activa-api client against stubbed responses.

The other Merit tests replace the library client with a fake. These keep the library in the
loop, so they also cover what it sends on the wire and how its errors reach our callers.
All data is made up.
"""

import json

import pytest
import responses

from firma_mcp.errors import ConfigError, SourceError
from firma_mcp.modules import merit as module
from firma_mcp.modules.merit.client import MeritReader, make_client

BASE = "https://program.360ksiegowosc.pl/api"
JSON = {"Content-Type": "application/json"}


@pytest.fixture
def reader():
    return MeritReader(make_client("stub-id", "c3R1Yi1rZXk=", "pl"))


def body_of(call) -> dict:
    return json.loads(call.request.body)


@responses.activate
def test_purchases_posts_period_and_maps_rows(reader):
    responses.post(
        f"{BASE}/v1/getpurchorders",
        json=[
            {
                "PIHId": "p-1",
                "BillNo": "FV 12/2026",
                "VendorName": "Hosting Example",
                "VatRegNo": "2222222222",
                "VendorRegNo": "222222222",
                "DocumentDate": "2026-09-01T00:00:00",
                "DueDate": "2026-09-15T00:00:00",
                "TotalSum": 123.45,
                "PaidAmount": 0,
                "Paid": False,
                "CurrencyCode": "PLN",
                "FileExists": True,
            }
        ],
        headers=JSON,
    )
    out = reader.purchases(7, unpaid_only=True)
    assert out[0]["vendor_nip"] == "2222222222" and out[0]["total"] == "123.45" and out[0]["paid"] is False
    sent = body_of(responses.calls[0])
    assert sent["UnPaid"] is True and len(sent["PeriodStart"]) == 8 and len(sent["PeriodEnd"]) == 8
    url = responses.calls[0].request.url
    assert "ApiId=stub-id" in url and "signature=" in url


@responses.activate
def test_sales_and_payments(reader):
    responses.post(
        f"{BASE}/v1/getinvoices",
        json=[
            {
                "SIHId": "s-1",
                "InvoiceNo": "1/09/2026",
                "CustomerName": "Customer",
                "TotalSum": 1000,
                "Paid": True,
            }
        ],
        headers=JSON,
    )
    responses.post(
        f"{BASE}/v1/getpayments",
        json=[{"PHId": "ph-1", "Direction": 1, "Amount": 1000, "CounterPartName": "Customer"}],
        headers=JSON,
    )
    assert reader.sales(7, False)[0]["number"] == "1/09/2026"
    payment = reader.payments(31)[0]
    assert payment["direction_label"] == "wpływ" and payment["amount"] == "1000.00"
    assert reader.find_payment("1000.00", counterparty="customer")["found"] is True


@responses.activate
def test_invoice_details_use_the_id_parameter(reader):
    responses.post(f"{BASE}/v2/getpurchorder", json={"Header": {"PIHId": "p-1"}, "Lines": []}, headers=JSON)
    responses.post(f"{BASE}/v2/getinvoice", json={"Header": {"SIHId": "s-1"}, "Lines": []}, headers=JSON)
    assert reader.invoice("purchase", "p-1")["Header"]["PIHId"] == "p-1"
    assert body_of(responses.calls[0]) == {"Id": "p-1", "SkipAttachment": True}
    assert reader.invoice("sales", "s-1")["Header"]["SIHId"] == "s-1"
    assert body_of(responses.calls[1]) == {"Id": "s-1"}


@responses.activate
def test_only_read_endpoints_are_called(reader):
    for version, endpoint in [
        ("v1", "getpurchorders"),
        ("v1", "getinvoices"),
        ("v1", "getpayments"),
        ("v2", "getpurchorder"),
        ("v2", "getinvoice"),
    ]:
        responses.post(f"{BASE}/{version}/{endpoint}", json=[] if version == "v1" else {}, headers=JSON)
    reader.purchases(7, False)
    reader.sales(7, False)
    reader.payments(7)
    reader.purchase_detail("p-1")
    reader.invoice("sales", "s-1")
    endpoints = {call.request.url.split("?")[0].rsplit("/", 1)[1] for call in responses.calls}
    assert endpoints == {"getpurchorders", "getinvoices", "getpayments", "getpurchorder", "getinvoice"}
    assert all(name.startswith("get") for name in endpoints)


@responses.activate
def test_wrong_credentials_become_source_error(reader):
    responses.post(f"{BASE}/v1/getpurchorders", status=401, body="Unauthorized")
    with pytest.raises(SourceError) as exc:
        reader.purchases(7, False)
    assert exc.value.source == "merit" and "MeritAuthError" in exc.value.message


@responses.activate
def test_rate_limit_is_reported_with_its_status(reader):
    responses.post(f"{BASE}/v1/getpurchorders", status=429, body="Too many requests")
    with pytest.raises(SourceError, match="429"):
        reader.purchases(7, False)


@responses.activate
def test_plain_text_answer_is_source_error(reader):
    responses.post(f"{BASE}/v1/getpayments", body="OK", headers={"Content-Type": "text/plain"})
    with pytest.raises(SourceError, match="expected a list"):
        reader.payments(7)


def test_load_reads_group_and_country():
    assert module.load({}) is None
    with pytest.raises(ConfigError, match="MERIT_API_KEY"):
        module.load({"MERIT_API_ID": "id"})
    config = module.load({"MERIT_API_ID": "id", "MERIT_API_KEY": "key"})
    assert config.country == "pl"
    with pytest.raises(ConfigError, match="MERIT_COUNTRY"):
        module.load({"MERIT_API_ID": "id", "MERIT_API_KEY": "key", "MERIT_COUNTRY": "de"})
