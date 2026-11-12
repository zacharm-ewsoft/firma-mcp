import datetime as dt
import re
from pathlib import Path

import pytest

from firma_mcp.errors import SourceError
from firma_mcp.modules.merit import client as merit


class FakeMerit:
    def __init__(self):
        self.calls = []
        self.purchase_rows = [
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
        ]
        self.sales_rows = [
            {
                "SIHId": "s-1",
                "InvoiceNo": "1/09/2026",
                "CustomerName": "Customer",
                "VatRegNo": "1234567890",
                "RegNo": "123456785",
                "DocumentDate": "2026-09-02T00:00:00",
                "DueDate": "2026-09-16T00:00:00",
                "TotalSum": 1000,
                "PaidAmount": 1000,
                "Paid": True,
                "CurrencyCode": "PLN",
            }
        ]
        self.payment_rows = [
            {
                "PHId": "ph-1",
                "DocumentDate": "2026-09-05T00:00:00",
                "BankName": "Example Bank",
                "CounterPartName": "Hosting Example",
                "CounterPartType": "Vendor",
                "CounterPartId": "c-1",
                "Direction": 2,
                "Amount": 123.45,
                "CurrencyCode": "PLN",
                "DocumentNo": "WB 9/2026",
                "ChangedDate": "2026-09-05T00:00:00",
                "CurrencyRate": 1,
            }
        ]

    def get_payments(self, period_start, period_end):
        self.calls.append(("payments", period_start, period_end))
        return self.payment_rows

    def get_purchase_invoices(self, period_start, period_end, unpaid=False):
        self.calls.append(("purch", period_start, period_end, unpaid))
        return self.purchase_rows

    def get_invoices(self, period_start, period_end, unpaid=False, department_code=None):
        self.calls.append(("sales", period_start, period_end, unpaid))
        return self.sales_rows

    def _request(self, endpoint, payload=None, version="v1", params=None):
        self.calls.append(("_request", endpoint, payload, version))
        if endpoint == "getpurchorder":
            return {
                "Header": {"PIHId": payload["Id"]},
                "Lines": [],
                "Attachment": {"Filename": "a.pdf", "FileContent": "AAA="},
            }
        if endpoint == "getinvoice":
            return {"Header": {"SIHId": payload["Id"]}, "Lines": []}
        raise AssertionError(f"unexpected endpoint {endpoint!r}")


def test_period_formats_yyyymmdd():
    start, end = merit.period(7, today=dt.date(2026, 9, 10))
    assert (start, end) == ("20260903", "20260910")


def test_money():
    assert merit.money(123.45) == "123.45"
    assert merit.money(1000) == "1000.00"
    assert merit.money(None) is None


def test_map_purchase():
    row = FakeMerit().purchase_rows[0]
    out = merit.map_purchase(row)
    assert out == {
        "id": "p-1",
        "number": "FV 12/2026",
        "vendor": "Hosting Example",
        "vendor_nip": "2222222222",
        "vendor_regon": "222222222",
        "document_date": "2026-09-01",
        "due_date": "2026-09-15",
        "total": "123.45",
        "paid_amount": "0.00",
        "paid": False,
        "currency": "PLN",
        "has_pdf": True,
    }


def test_map_purchase_vendor_nip_prefers_vat_reg_no_over_vendor_reg_no():
    # VendorRegNo is the REGON/KRS number (9 digits) and the NIP is in VatRegNo (10 digits). On live
    # data, mixing them up put the REGON into vendor_nip and broke every NIP comparison.
    row = {"PIHId": "p-2", "VendorName": "Vendor", "VatRegNo": "3333333333", "VendorRegNo": "222222222"}
    out = merit.map_purchase(row)
    assert out["vendor_nip"] == "3333333333"
    assert out["vendor_regon"] == "222222222"


def test_map_purchase_vendor_nip_falls_back_to_vendor_reg_no_when_vat_missing():
    row = {"PIHId": "p-3", "VendorName": "Vendor", "VendorRegNo": "222222222"}
    out = merit.map_purchase(row)
    assert out["vendor_nip"] == "222222222"
    assert out["vendor_regon"] == "222222222"


def test_map_sale_customer_nip_prefers_vat_reg_no_over_reg_no():
    row = {"SIHId": "s-2", "CustomerName": "Customer", "VatRegNo": "3333333333", "RegNo": "123456785"}
    out = merit.map_sale(row)
    assert out["customer_nip"] == "3333333333"
    assert out["customer_regon"] == "123456785"


def test_purchases_passes_unpaid_and_period():
    client = FakeMerit()
    out = merit.MeritReader(client).purchases(days=7, unpaid_only=True)
    assert out[0]["vendor"] == "Hosting Example"
    kind, start, end, unpaid = client.calls[0]
    assert kind == "purch" and unpaid is True and len(start) == 8 and len(end) == 8


def test_purchases_between_uses_given_dates():
    client = FakeMerit()
    out = merit.MeritReader(client).purchases_between("20260601", "20260610", False)
    assert out[0]["vendor"] == "Hosting Example"
    kind, start, end, unpaid = client.calls[0]
    assert (kind, start, end, unpaid) == ("purch", "20260601", "20260610", False)


def test_purchase_detail_returns_lines():
    client = FakeMerit()
    out = merit.MeritReader(client).purchase_detail("p-1")
    assert out["Header"]["PIHId"] == "p-1"
    assert ("_request", "getpurchorder", {"Id": "p-1", "SkipAttachment": True}, "v2") in client.calls


def test_purchase_detail_rejects_non_dict_payload():
    class BadDetails(FakeMerit):
        def _request(self, endpoint, payload=None, version="v1", params=None):
            return "OK"

    with pytest.raises(SourceError) as exc:
        merit.MeritReader(BadDetails()).purchase_detail("p-1")
    assert exc.value.source == "merit"


def test_sales_maps_customer():
    out = merit.MeritReader(FakeMerit()).sales(days=7, unpaid_only=False)
    assert out[0]["customer"] == "Customer"
    assert out[0]["number"] == "1/09/2026"
    assert out[0]["paid"] is True


def test_invoice_strips_attachment_unless_requested():
    client = FakeMerit()
    out = merit.MeritReader(client).invoice("purchase", "p-1")
    assert "Attachment" not in out
    out_pdf = merit.MeritReader(client).invoice("purchase", "p-1", with_pdf=True)
    assert out_pdf["Attachment"]["Filename"] == "a.pdf"


def test_invoice_purchase_calls_request_with_id_param():
    client = FakeMerit()
    merit.MeritReader(client).invoice("purchase", "p-1")
    assert ("_request", "getpurchorder", {"Id": "p-1", "SkipAttachment": True}, "v2") in client.calls


def test_invoice_purchase_with_pdf_keeps_attachment_in_request():
    client = FakeMerit()
    merit.MeritReader(client).invoice("purchase", "p-1", with_pdf=True)
    assert ("_request", "getpurchorder", {"Id": "p-1", "SkipAttachment": False}, "v2") in client.calls


def test_invoice_sales_calls_request_with_id_param():
    client = FakeMerit()
    merit.MeritReader(client).invoice("sales", "s-1")
    assert ("_request", "getinvoice", {"Id": "s-1"}, "v2") in client.calls


def test_invoice_with_pdf_short_content_is_not_truncated():
    out = merit.MeritReader(FakeMerit()).invoice("purchase", "p-1", with_pdf=True)
    assert out["Attachment"]["FileContent"] == "AAA="
    assert out["Attachment"]["truncated"] is False


def test_invoice_with_pdf_long_content_is_capped():
    class BigAttachment(FakeMerit):
        def _request(self, endpoint, payload=None, version="v1", params=None):
            return {
                "Header": {"PIHId": payload["Id"]},
                "Lines": [],
                "Attachment": {"Filename": "a.pdf", "FileContent": "A" * (merit.MAX_PDF_B64_CHARS + 500)},
            }

    out = merit.MeritReader(BigAttachment()).invoice("purchase", "p-1", with_pdf=True)
    assert len(out["Attachment"]["FileContent"]) == merit.MAX_PDF_B64_CHARS
    assert out["Attachment"]["truncated"] is True


def test_invoice_bad_kind():
    with pytest.raises(SourceError, match="kind"):
        merit.MeritReader(FakeMerit()).invoice("other", "x")


def test_api_errors_become_source_error():
    class Broken(FakeMerit):
        def get_purchase_invoices(self, *a, **k):
            raise RuntimeError("HTTP 500")

    with pytest.raises(SourceError) as exc:
        merit.MeritReader(Broken()).purchases(days=3, unpaid_only=False)
    assert exc.value.source == "merit"


def test_purchases_rejects_non_list_payload():
    class BadShape(FakeMerit):
        def get_purchase_invoices(self, *a, **k):
            return "OK"

    with pytest.raises(SourceError) as exc:
        merit.MeritReader(BadShape()).purchases(days=3, unpaid_only=False)
    assert exc.value.source == "merit"


def test_purchases_skips_non_dict_rows():
    class NoneRows(FakeMerit):
        def get_purchase_invoices(self, *a, **k):
            return [None, self.purchase_rows[0]]

    out = merit.MeritReader(NoneRows()).purchases(days=3, unpaid_only=False)
    assert len(out) == 1
    assert out[0]["vendor"] == "Hosting Example"


def test_invoice_rejects_non_dict_payload():
    class BadDetails(FakeMerit):
        def _request(self, endpoint, payload=None, version="v1", params=None):
            return "OK"

    with pytest.raises(SourceError) as exc:
        merit.MeritReader(BadDetails()).invoice("sales", "s-1")
    assert exc.value.source == "merit"


def test_module_uses_no_write_methods():
    source = Path(merit.__file__).read_text(encoding="utf-8")
    forbidden = re.findall(r"\.(create_\w+|send_\w+|delete_\w+|update_\w+|add_\w+)\(", source)
    assert forbidden == []


# -- merit_payments ------------------------------------------------------------


def test_payments_maps_fields():
    out = merit.MeritReader(FakeMerit()).payments(days=31)
    assert out == [
        {
            "id": "ph-1",
            "date": "2026-09-05",
            "bank": "Example Bank",
            "counterparty": "Hosting Example",
            "counterparty_type": "Vendor",
            "direction": 2,
            "direction_label": "wypływ",
            "amount": "123.45",
            "currency": "PLN",
            "document_no": "WB 9/2026",
        }
    ]


@pytest.mark.parametrize("code,label", [(1, "wpływ"), (2, "wypływ"), (0, "kompensata"), (99, "nieznany")])
def test_map_payment_direction_label(code, label):
    out = merit.map_payment({"PHId": "ph-x", "Direction": code, "Amount": 1})
    assert out["direction"] == code
    assert out["direction_label"] == label


def test_payments_passes_period():
    client = FakeMerit()
    merit.MeritReader(client).payments(days=45)
    kind, start, end = client.calls[0]
    assert kind == "payments" and len(start) == 8 and len(end) == 8


def test_payments_rejects_non_list_payload():
    class BadShape(FakeMerit):
        def get_payments(self, *a, **k):
            return "OK"

    with pytest.raises(SourceError) as exc:
        merit.MeritReader(BadShape()).payments(days=31)
    assert exc.value.source == "merit"


def test_payments_skips_non_dict_rows():
    class NoneRows(FakeMerit):
        def get_payments(self, *a, **k):
            return [None, self.payment_rows[0]]

    out = merit.MeritReader(NoneRows()).payments(days=31)
    assert len(out) == 1


def test_payments_api_error_becomes_source_error():
    class Broken(FakeMerit):
        def get_payments(self, *a, **k):
            raise RuntimeError("HTTP 500")

    with pytest.raises(SourceError) as exc:
        merit.MeritReader(Broken()).payments(days=31)
    assert exc.value.source == "merit"


# -- payment_check -------------------------------------------------------------


def test_find_payment_matches_by_amount():
    out = merit.MeritReader(FakeMerit()).find_payment(amount="123.45", days=45)
    assert out["found"] is True
    assert out["checked_days"] == 45
    assert len(out["matches"]) == 1
    assert out["matches"][0]["counterparty"] == "Hosting Example"


def test_find_payment_amount_tolerance():
    out = merit.MeritReader(FakeMerit()).find_payment(amount="123.44", days=45)
    assert out["found"] is True


def test_find_payment_no_match_amount():
    out = merit.MeritReader(FakeMerit()).find_payment(amount="999.00", days=45)
    assert out["found"] is False
    assert out["matches"] == []


def test_find_payment_filters_by_counterparty_substring_case_insensitive():
    out = merit.MeritReader(FakeMerit()).find_payment(amount="123.45", counterparty="hosting", days=45)
    assert out["found"] is True
    out_no = merit.MeritReader(FakeMerit()).find_payment(amount="123.45", counterparty="Telecom", days=45)
    assert out_no["found"] is False


def test_find_payment_nip_is_ignored_for_matching():
    # Merit payments carry no NIP: nip is accepted but does not affect matching.
    out = merit.MeritReader(FakeMerit()).find_payment(amount="123.45", nip="2222222222", days=45)
    assert out["found"] is True


def test_find_payment_invalid_amount_is_source_error():
    with pytest.raises(SourceError):
        merit.MeritReader(FakeMerit()).find_payment(amount="not-a-number", days=45)


def test_find_payment_uses_full_requested_period():
    client = FakeMerit()
    merit.MeritReader(client).find_payment(amount="123.45", days=45)
    kind, start, end = client.calls[0]
    delta = (dt.datetime.strptime(end, "%Y%m%d") - dt.datetime.strptime(start, "%Y%m%d")).days
    assert delta == 45
