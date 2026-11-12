import datetime as dt
import re
from pathlib import Path

import pytest

from firma_mcp.errors import SourceError
from firma_mcp.modules.merit import suggestions
from firma_mcp.modules.merit.client import MeritReader


def reader_for(client) -> MeritReader:
    """One reader per fake client, so the history cache (keyed by reader) behaves as in the server."""
    if not hasattr(client, "_reader"):
        client._reader = MeritReader(client)
    return client._reader


@pytest.fixture(autouse=True)
def _clear_cache():
    suggestions.clear_cache()
    yield
    suggestions.clear_cache()


class FakeClient:
    """Two vendors: A has a consistent account (3/3), B has split accounts (2/3)."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self._request_calls: list[tuple[str, dict]] = []
        # VatRegNo is the NIP (used for matching) and VendorRegNo the REGON. They differ on purpose
        # in every row, so the test really checks that VatRegNo takes priority and does not pass
        # by accident if the code read the wrong field.
        self.purchase_rows = [
            {
                "PIHId": "a-1",
                "BillNo": "FA/1",
                "VendorName": "Vendor A",
                "VatRegNo": "1111111111",
                "VendorRegNo": "111000111",
                "TotalSum": 100,
                "Paid": True,
                "CurrencyCode": "PLN",
            },
            {
                "PIHId": "a-2",
                "BillNo": "FA/2",
                "VendorName": "Vendor A",
                "VatRegNo": "1111111111",
                "VendorRegNo": "111000111",
                "TotalSum": 100,
                "Paid": True,
                "CurrencyCode": "PLN",
            },
            {
                "PIHId": "a-3",
                "BillNo": "FA/3",
                "VendorName": "Vendor A",
                "VatRegNo": "1111111111",
                "VendorRegNo": "111000111",
                "TotalSum": 100,
                "Paid": True,
                "CurrencyCode": "PLN",
            },
            {
                "PIHId": "b-1",
                "BillNo": "FB/1",
                "VendorName": "Vendor B",
                "VatRegNo": "2222222222",
                "VendorRegNo": "222000222",
                "TotalSum": 200,
                "Paid": True,
                "CurrencyCode": "PLN",
            },
            {
                "PIHId": "b-2",
                "BillNo": "FB/2",
                "VendorName": "Vendor B",
                "VatRegNo": "2222222222",
                "VendorRegNo": "222000222",
                "TotalSum": 200,
                "Paid": True,
                "CurrencyCode": "PLN",
            },
            {
                "PIHId": "b-3",
                "BillNo": "FB/3",
                "VendorName": "Vendor B",
                "VatRegNo": "2222222222",
                "VendorRegNo": "222000222",
                "TotalSum": 200,
                "Paid": True,
                "CurrencyCode": "PLN",
            },
        ]
        self.details = {
            "a-1": {"Lines": [{"AccountCode": "501", "TaxName": "23%"}]},
            "a-2": {"Lines": [{"AccountCode": "501", "TaxName": "23%"}]},
            "a-3": {"Lines": [{"AccountCode": "501", "TaxName": "23%"}]},
            "b-1": {"Lines": [{"AccountCode": "502", "TaxName": "23%"}]},
            "b-2": {"Lines": [{"AccountCode": "502", "TaxName": "23%"}]},
            "b-3": {"Lines": [{"AccountCode": "503", "TaxName": "8%"}]},
        }

    def get_purchase_invoices(self, start, end, unpaid=False):
        self.calls.append((start, end))
        return self.purchase_rows

    def _request(self, endpoint, payload=None, version="v1", params=None):
        self._request_calls.append((endpoint, dict(payload or {})))
        if endpoint == "getpurchorder":
            return self.details[payload["Id"]]
        raise AssertionError(f"unexpected endpoint {endpoint!r}")


TODAY = dt.date(2026, 9, 11)
NO_SLEEP = lambda seconds: None  # noqa: E731 - stands in for pauses and backoff


def test_history_dedupes_and_collects_lines():
    client = FakeClient()
    result = suggestions.history(reader_for(client), today=TODAY, sleep=NO_SLEEP)
    rows = result.rows
    assert len(rows) == 6  # 6 unique invoices, one line each
    assert {r.invoice_id for r in rows} == {"a-1", "a-2", "a-3", "b-1", "b-2", "b-3"}
    a_rows = [r for r in rows if r.vendor_nip == "1111111111"]
    assert all(r.account_code == "501" and r.tax_name == "23%" for r in a_rows)
    assert result.truncated is False
    assert result.invoices_used == 6


def test_history_windows_are_at_most_89_days():
    client = FakeClient()
    suggestions.history(reader_for(client), today=TODAY, sleep=NO_SLEEP)
    assert client.calls, "expected at least one window call"
    for start, end in client.calls:
        delta = (dt.datetime.strptime(end, "%Y%m%d") - dt.datetime.strptime(start, "%Y%m%d")).days
        assert 0 <= delta <= 89


def test_history_caches_result_for_same_client():
    client = FakeClient()
    result1 = suggestions.history(reader_for(client), today=TODAY, sleep=NO_SLEEP)
    calls_after_first = len(client.calls)
    requests_after_first = len(client._request_calls)
    result2 = suggestions.history(reader_for(client), today=TODAY, sleep=NO_SLEEP)
    assert result1 == result2
    assert len(client.calls) == calls_after_first
    assert len(client._request_calls) == requests_after_first


def test_clear_cache_forces_refetch():
    client = FakeClient()
    suggestions.history(reader_for(client), today=TODAY, sleep=NO_SLEEP)
    calls_after_first = len(client.calls)
    suggestions.clear_cache()
    suggestions.history(reader_for(client), today=TODAY, sleep=NO_SLEEP)
    assert len(client.calls) > calls_after_first


def test_suggest_consistent_vendor_full_confidence():
    client = FakeClient()
    rows = suggestions.history(reader_for(client), today=TODAY, sleep=NO_SLEEP).rows
    ksef_invoices = [
        {
            "ksef_number": "K-A",
            "invoice_number": "FA/9",
            "seller_nip": "1111111111",
            "seller_name": "Vendor A",
            "gross": "123.00",
            "currency": "PLN",
            "ksef_date": "2026-09-01",
        }
    ]
    out = suggestions.suggest(ksef_invoices, rows)
    assert len(out) == 1
    item = out[0]
    assert item["ksef_number"] == "K-A"
    assert item["seller_nip"] == "1111111111"
    assert item["gross"] == "123.00"
    assert item["account"] == "501"
    assert item["vat_scheme"] == "23%"
    assert item["confidence"] == "3/3"
    assert item["basis"] == 3
    assert item["note"] is None


def test_suggest_mixed_vendor_partial_confidence():
    client = FakeClient()
    rows = suggestions.history(reader_for(client), today=TODAY, sleep=NO_SLEEP).rows
    ksef_invoices = [
        {
            "ksef_number": "K-B",
            "invoice_number": "FB/9",
            "seller_nip": "2222222222",
            "seller_name": "Vendor B",
            "gross": "456.00",
            "currency": "PLN",
            "ksef_date": "2026-09-02",
        }
    ]
    out = suggestions.suggest(ksef_invoices, rows)
    item = out[0]
    assert item["account"] == "502"
    assert item["confidence"] == "2/3"
    assert item["basis"] == 3
    assert item["note"] is None


def test_suggest_unknown_vendor_has_no_history():
    client = FakeClient()
    rows = suggestions.history(reader_for(client), today=TODAY, sleep=NO_SLEEP).rows
    ksef_invoices = [
        {
            "ksef_number": "K-C",
            "invoice_number": "FC/9",
            "seller_nip": "3333333333",
            "seller_name": "Vendor C",
            "gross": "789.00",
            "currency": "PLN",
            "ksef_date": "2026-09-03",
        }
    ]
    out = suggestions.suggest(ksef_invoices, rows)
    item = out[0]
    assert item["account"] is None
    assert item["confidence"] is None
    assert item["basis"] == 0
    assert "brak historii" in item["note"]


def test_suggest_matches_nip_ignoring_formatting():
    client = FakeClient()
    rows = suggestions.history(reader_for(client), today=TODAY, sleep=NO_SLEEP).rows
    ksef_invoices = [
        {
            "ksef_number": "K-A2",
            "invoice_number": "FA/10",
            "seller_nip": "111-111-11-11",
            "seller_name": "Vendor A",
            "gross": "1.00",
            "currency": "PLN",
            "ksef_date": "2026-09-01",
        }
    ]
    out = suggestions.suggest(ksef_invoices, rows)
    assert out[0]["account"] == "501"


class ManyInvoicesFakeClient:
    """`n` invoices from one vendor, each with a different DocumentDate, for the fetch-cap test."""

    def __init__(self, n: int):
        self.request_calls: list[tuple[str, dict]] = []
        self.purchase_rows = [
            {
                "PIHId": f"inv-{i}",
                "BillNo": f"F/{i}",
                "VendorName": "Big Vendor",
                "VatRegNo": "1234567890",
                "VendorRegNo": "222222222",
                "DocumentDate": (dt.date(2026, 1, 1) + dt.timedelta(days=i)).isoformat(),
                "TotalSum": 10,
                "Paid": True,
                "CurrencyCode": "PLN",
            }
            for i in range(n)
        ]

    def get_purchase_invoices(self, start, end, unpaid=False):
        return self.purchase_rows

    def _request(self, endpoint, payload=None, version="v1", params=None):
        self.request_calls.append((endpoint, dict(payload or {})))
        if endpoint == "getpurchorder":
            return {"Lines": [{"AccountCode": "501", "TaxName": "23%"}]}
        raise AssertionError(f"unexpected endpoint {endpoint!r}")


def test_history_caps_detail_fetches_and_flags_truncated():
    client = ManyInvoicesFakeClient(160)
    result = suggestions.history(reader_for(client), today=TODAY, sleep=NO_SLEEP)
    detail_calls = [c for c in client.request_calls if c[0] == "getpurchorder"]
    assert len(detail_calls) == suggestions.MAX_DETAIL_FETCHES == 150
    assert result.truncated is True
    assert result.invoices_used == 150
    assert len(result.rows) == 150  # one line per invoice in this fake
    assert result.partial is False


def test_history_under_cap_is_not_truncated():
    client = ManyInvoicesFakeClient(150)
    result = suggestions.history(reader_for(client), today=TODAY, sleep=NO_SLEEP)
    assert result.truncated is False
    assert result.invoices_used == 150
    assert result.partial is False


def test_history_paces_detail_fetches():
    # FakeClient has 6 unique invoices: 5 pauses between them (none after the last).
    client = FakeClient()
    sleeps: list[float] = []
    result = suggestions.history(reader_for(client), today=TODAY, sleep=sleeps.append)
    assert result.invoices_used == 6
    assert sleeps == [suggestions.DETAIL_FETCH_INTERVAL] * 5


# -- 429 (Merit rate limit): retries with backoff, partial result -------------

RATE_LIMIT_MESSAGE = 'API error 429: "Too many requests"'


class FlakyDetailClient:
    """One invoice; its getpurchorder answers 429 `fail_times` times, then succeeds."""

    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.detail_calls = 0
        self.purchase_rows = [
            {
                "PIHId": "x-1",
                "BillNo": "FX/1",
                "VendorName": "Vendor X",
                "VatRegNo": "1111111111",
                "VendorRegNo": "111000111",
                "TotalSum": 10,
                "Paid": True,
                "CurrencyCode": "PLN",
            },
        ]

    def get_purchase_invoices(self, start, end, unpaid=False):
        return self.purchase_rows

    def _request(self, endpoint, payload=None, version="v1", params=None):
        if endpoint != "getpurchorder":
            raise AssertionError(f"unexpected endpoint {endpoint!r}")
        self.detail_calls += 1
        if self.detail_calls <= self.fail_times:
            raise RuntimeError(RATE_LIMIT_MESSAGE)
        return {"Lines": [{"AccountCode": "501", "TaxName": "23%"}]}


def test_history_retries_detail_fetch_on_429_then_succeeds():
    client = FlakyDetailClient(fail_times=2)
    sleeps: list[float] = []
    result = suggestions.history(reader_for(client), today=TODAY, sleep=sleeps.append)
    assert client.detail_calls == 3
    assert sleeps == [2, 4]
    assert result.partial is False
    assert result.truncated is False
    assert len(result.rows) == 1


class PersistentFailClient:
    """Three invoices from one vendor (DocumentDate sets the fetch order, newest first);
    getpurchorder always answers 429 for the ids in `fail_ids`."""

    def __init__(self, fail_ids: set[str]):
        self.fail_ids = fail_ids
        self.detail_calls: list[str] = []
        self.purchase_rows = [
            {
                "PIHId": "inv-1",
                "BillNo": "F/1",
                "VendorName": "Vendor X",
                "VatRegNo": "1111111111",
                "VendorRegNo": "111000111",
                "DocumentDate": "2026-09-03",
                "TotalSum": 10,
                "Paid": True,
                "CurrencyCode": "PLN",
            },
            {
                "PIHId": "inv-2",
                "BillNo": "F/2",
                "VendorName": "Vendor X",
                "VatRegNo": "1111111111",
                "VendorRegNo": "111000111",
                "DocumentDate": "2026-09-02",
                "TotalSum": 10,
                "Paid": True,
                "CurrencyCode": "PLN",
            },
            {
                "PIHId": "inv-3",
                "BillNo": "F/3",
                "VendorName": "Vendor X",
                "VatRegNo": "1111111111",
                "VendorRegNo": "111000111",
                "DocumentDate": "2026-09-01",
                "TotalSum": 10,
                "Paid": True,
                "CurrencyCode": "PLN",
            },
        ]

    def get_purchase_invoices(self, start, end, unpaid=False):
        return self.purchase_rows

    def _request(self, endpoint, payload=None, version="v1", params=None):
        if endpoint != "getpurchorder":
            raise AssertionError(f"unexpected endpoint {endpoint!r}")
        pih_id = payload["Id"]
        self.detail_calls.append(pih_id)
        if pih_id in self.fail_ids:
            raise RuntimeError(RATE_LIMIT_MESSAGE)
        return {"Lines": [{"AccountCode": "501", "TaxName": "23%"}]}


def test_history_returns_partial_result_when_429_persists_mid_build():
    # inv-1 and inv-2 (the newest) are fetched normally; inv-3 keeps answering 429 (6 attempts:
    # the first plus 5 backoffs) and the build stops, returning what was collected so far.
    client = PersistentFailClient(fail_ids={"inv-3"})
    sleeps: list[float] = []
    result = suggestions.history(reader_for(client), today=TODAY, sleep=sleeps.append)
    assert result.partial is True
    assert result.truncated is True
    assert result.invoices_used == 2
    assert len(result.rows) == 2
    assert {r.invoice_id for r in result.rows} == {"inv-1", "inv-2"}
    assert client.detail_calls.count("inv-3") == 1 + len(suggestions.RATE_LIMIT_BACKOFFS)


def test_history_raises_when_429_persists_from_the_start():
    # No rows collected before the persistent 429, so there is no meaningful partial result.
    client = PersistentFailClient(fail_ids={"inv-1", "inv-2", "inv-3"})
    with pytest.raises(SourceError):
        suggestions.history(reader_for(client), today=TODAY, sleep=NO_SLEEP)


class FlakyWindowClient(FakeClient):
    """Like FakeClient, but the first get_purchase_invoices call (any window) answers 429 once."""

    def __init__(self):
        super().__init__()
        self.window_call_count = 0

    def get_purchase_invoices(self, start, end, unpaid=False):
        self.window_call_count += 1
        if self.window_call_count == 1:
            raise RuntimeError(RATE_LIMIT_MESSAGE)
        return super().get_purchase_invoices(start, end, unpaid)


def test_history_retries_window_list_call_on_429():
    client = FlakyWindowClient()
    sleeps: list[float] = []
    result = suggestions.history(reader_for(client), today=TODAY, sleep=sleeps.append)
    assert sleeps[0] == 2  # backoff after the first failed window-list attempt
    assert result.invoices_used == 6
    assert len(result.rows) == 6


def test_module_uses_no_write_methods():
    source = Path(suggestions.__file__).read_text(encoding="utf-8")
    forbidden = re.findall(r"\.(create_\w+|send_\w+|delete_\w+|update_\w+|add_\w+)\(", source)
    assert forbidden == []
