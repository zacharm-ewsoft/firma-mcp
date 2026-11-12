import base64
import datetime as dt
import json

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

from firma_mcp.errors import SourceError
from firma_mcp.modules.ksef.client import KsefClient, encrypt_token


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ksef-test")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    der_b64 = base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode()
    return key, der_b64


def test_encrypt_token_roundtrip(keypair):
    key, der_b64 = keypair
    blob = encrypt_token("secret-token", 1700000000000, der_b64)
    plain = key.decrypt(
        base64.b64decode(blob),
        padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    assert plain == b"secret-token|1700000000000"


class Server:
    """A simulated KSeF: records requests and answers according to the scenario."""

    def __init__(self, der_b64, status_codes=(200,), token_valid=True, pages=None, metadata_body=None):
        self.der_b64 = der_b64
        self.status_codes = list(status_codes)
        self.token_valid = token_valid
        self.pages = pages or [{"hasMore": False, "isTruncated": False, "invoices": []}]
        self.metadata_body = metadata_body
        self.requests = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/auth/challenge"):
            return httpx.Response(
                200,
                json={"challenge": "CH-1", "timestamp": "2026-09-10T00:00:00Z", "timestampMs": 1700000000000},
            )
        if path.endswith("/security/public-key-certificates"):
            return httpx.Response(
                200,
                json=[
                    {
                        "certificate": self.der_b64,
                        "certificateId": "c1",
                        "publicKeyId": "k1",
                        "usage": ["KsefTokenEncryption"],
                        "validFrom": "2026-01-01T00:00:00Z",
                        "validTo": "2030-01-01T00:00:00Z",
                    }
                ],
            )
        if path.endswith("/auth/ksef-token"):
            body = json.loads(request.content)
            assert body["contextIdentifier"] == {"type": "Nip", "value": "1111111111"}
            assert body["publicKeyId"] == "k1"
            return httpx.Response(
                202, json={"referenceNumber": "REF-1", "authenticationToken": {"token": "AUTH-T"}}
            )
        if path.endswith("/auth/REF-1"):
            assert request.headers["authorization"] == "Bearer AUTH-T"
            code = self.status_codes.pop(0) if len(self.status_codes) > 1 else self.status_codes[0]
            return httpx.Response(200, json={"status": {"code": code, "description": "x"}})
        if path.endswith("/auth/token/redeem"):
            return httpx.Response(
                200,
                json={
                    "accessToken": {"token": "ACCESS-T", "validUntil": "2026-09-10T01:00:00Z"},
                    "refreshToken": {"token": "REFRESH-T"},
                },
            )
        if path.endswith("/invoices/query/metadata"):
            if request.headers.get("authorization") != "Bearer ACCESS-T" or not self.token_valid:
                self.token_valid = True  # works after re-authentication
                return httpx.Response(401, json={"title": "Unauthorized"})
            if self.metadata_body is not None:
                return httpx.Response(200, json=self.metadata_body)
            offset = int(request.url.params.get("pageOffset", "0"))
            return httpx.Response(200, json=self.pages[min(offset, len(self.pages) - 1)])
        if "/invoices/ksef/" in path:
            return httpx.Response(
                200, content=b"<Faktura>1</Faktura>", headers={"content-type": "application/xml"}
            )
        return httpx.Response(404)


def make_client(server, **kw):
    return KsefClient(
        "secret-token",
        "1111111111",
        env="test",
        transport=httpx.MockTransport(server.handler),
        sleep=lambda s: None,
        **kw,
    )


def test_authenticate_polls_until_200(keypair):
    server = Server(keypair[1], status_codes=(100, 100, 200))
    client = make_client(server)
    client.authenticate()
    paths = [r.url.path for r in server.requests]
    assert paths.count("/v2/auth/REF-1") == 3
    assert paths[-1] == "/v2/auth/token/redeem"


def test_authenticate_failure_status_raises(keypair):
    server = Server(keypair[1], status_codes=(400,))
    with pytest.raises(SourceError, match="authentication"):
        make_client(server).authenticate()


def test_received_maps_invoices_and_pages(keypair):
    inv = {
        "ksefNumber": "1111111111-20260909-ABC-1",
        "invoiceNumber": "FV 12/2026",
        "issueDate": "2026-09-08",
        "invoicingDate": "2026-09-09T10:00:00Z",
        "permanentStorageDate": "2026-09-09T10:05:00Z",
        "seller": {"nip": "2222222222", "name": "Hosting Example"},
        "buyer": {"identifier": {"type": "Nip", "value": "1111111111"}, "name": "Example Buyer"},
        "netAmount": 100.0,
        "grossAmount": 123.0,
        "vatAmount": 23.0,
        "currency": "PLN",
        "invoiceType": "Vat",
        "hasAttachment": False,
    }
    pages = [
        {"hasMore": True, "isTruncated": False, "invoices": [inv]},
        {"hasMore": False, "isTruncated": False, "invoices": [dict(inv, ksefNumber="X-2")]},
    ]
    server = Server(keypair[1], pages=pages)
    out = make_client(server).received(days=7)
    assert [i["ksef_number"] for i in out] == ["1111111111-20260909-ABC-1", "X-2"]
    assert out[0] == {
        "ksef_number": "1111111111-20260909-ABC-1",
        "invoice_number": "FV 12/2026",
        "seller_nip": "2222222222",
        "seller_name": "Hosting Example",
        "issue_date": "2026-09-08",
        "ksef_date": "2026-09-09T10:05:00Z",
        "gross": "123.00",
        "net": "100.00",
        "vat": "23.00",
        "currency": "PLN",
        "invoice_type": "Vat",
        "has_attachment": False,
    }
    query = next(r for r in server.requests if r.url.path.endswith("/invoices/query/metadata"))
    body = json.loads(query.content)
    assert body["subjectType"] == "Subject2"
    assert body["dateRange"]["dateType"] == "PermanentStorage"
    assert query.url.params["pageSize"] == "100"


def test_received_reauthenticates_on_401(keypair):
    server = Server(keypair[1], token_valid=False)
    out = make_client(server).received(days=3)
    assert out == []
    assert [r.url.path for r in server.requests].count("/v2/auth/token/redeem") == 2


def test_invoice_xml(keypair):
    server = Server(keypair[1])
    assert make_client(server).invoice_xml("X-1") == "<Faktura>1</Faktura>"


def test_days_over_100_are_capped(keypair):
    server = Server(keypair[1])
    make_client(server).received(days=365)
    query = next(r for r in server.requests if r.url.path.endswith("/invoices/query/metadata"))
    body = json.loads(query.content)
    frm = dt.datetime.fromisoformat(body["dateRange"]["from"])
    assert (dt.datetime.now(dt.UTC) - frm).days <= 100


def test_received_non_dict_metadata_body_raises(keypair):
    server = Server(keypair[1], metadata_body=["not", "a", "dict"])
    with pytest.raises(SourceError, match="metadata"):
        make_client(server).received(days=1)


# -- pagination safety ---------------------------------------------------------

_INV_A = {
    "ksefNumber": "A-1",
    "invoiceNumber": "FV1",
    "issueDate": "2026-09-08",
    "permanentStorageDate": "2026-09-09T10:05:00Z",
    "seller": {"nip": "1", "name": "S"},
    "netAmount": 10.0,
    "grossAmount": 12.3,
    "vatAmount": 2.3,
    "currency": "PLN",
    "invoiceType": "Vat",
    "hasAttachment": False,
}
_INV_B = {
    "ksefNumber": "A-2",
    "invoiceNumber": "FV2",
    "issueDate": "2026-09-09",
    "permanentStorageDate": "2026-09-09T10:06:00Z",
    "seller": {"nip": "1", "name": "S"},
    "netAmount": 20.0,
    "grossAmount": 24.6,
    "vatAmount": 4.6,
    "currency": "PLN",
    "invoiceType": "Vat",
    "hasAttachment": False,
}


class TruncatedPaginationServer(Server):
    """The first metadata page is truncated (isTruncated); the second completes the range."""

    def __init__(self, der_b64):
        super().__init__(der_b64)
        self.metadata_calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/invoices/query/metadata"):
            self.requests.append(request)
            if request.headers.get("authorization") != "Bearer ACCESS-T":
                return httpx.Response(401, json={"title": "Unauthorized"})
            self.metadata_calls += 1
            if self.metadata_calls == 1:
                return httpx.Response(200, json={"hasMore": True, "isTruncated": True, "invoices": [_INV_A]})
            return httpx.Response(200, json={"hasMore": False, "isTruncated": False, "invoices": [_INV_B]})
        return super().handler(request)


def test_received_truncated_page_advances_from_and_dedupes(keypair):
    server = TruncatedPaginationServer(keypair[1])
    out = make_client(server).received(days=1)
    assert [i["ksef_number"] for i in out] == ["A-1", "A-2"]  # no duplicates
    metadata_requests = [r for r in server.requests if r.url.path.endswith("/invoices/query/metadata")]
    assert len(metadata_requests) == 2
    second_from = json.loads(metadata_requests[1].content)["dateRange"]["from"]
    last_date = dt.datetime.fromisoformat(_INV_A["permanentStorageDate"])
    assert dt.datetime.fromisoformat(second_from) > last_date


class AlwaysMoreServer(Server):
    """hasMore always True, isTruncated False, empty page: a cheap fake for the page-limit test."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/invoices/query/metadata"):
            self.requests.append(request)
            if request.headers.get("authorization") != "Bearer ACCESS-T":
                return httpx.Response(401, json={"title": "Unauthorized"})
            return httpx.Response(200, json={"hasMore": True, "isTruncated": False, "invoices": []})
        return super().handler(request)


def test_received_raises_after_max_pages(keypair):
    server = AlwaysMoreServer(keypair[1])
    with pytest.raises(SourceError, match="page limit"):
        make_client(server).received(days=1)


# -- malformed amounts and invoice shapes ---------------------------------------


def test_received_empty_amount_falls_back_to_string(keypair):
    inv = dict(_INV_A, grossAmount="")
    pages = [{"hasMore": False, "isTruncated": False, "invoices": [inv]}]
    server = Server(keypair[1], pages=pages)
    out = make_client(server).received(days=1)
    assert out[0]["gross"] == ""


def test_received_malformed_seller_raises(keypair):
    inv = dict(_INV_A, seller="Hosting Example")
    pages = [{"hasMore": False, "isTruncated": False, "invoices": [inv]}]
    server = Server(keypair[1], pages=pages)
    with pytest.raises(SourceError, match="unexpected invoice shape"):
        make_client(server).received(days=1)


# -- caller headers preserved across a 401 retry --------------------------------


class HeaderRetryServer(Server):
    """GET /invoices/ksef/{n} answers 401 once, then 200; we check the caller's headers."""

    def __init__(self, der_b64):
        super().__init__(der_b64)
        self.ksef_calls = 0
        self.seen_headers: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/invoices/ksef/" in path:
            self.requests.append(request)
            self.ksef_calls += 1
            self.seen_headers.append(dict(request.headers))
            if self.ksef_calls == 1:
                return httpx.Response(401, json={"title": "Unauthorized"})
            return httpx.Response(
                200, content=b"<Faktura>1</Faktura>", headers={"content-type": "application/xml"}
            )
        return super().handler(request)


def test_request_preserves_caller_headers_on_retry(keypair):
    server = HeaderRetryServer(keypair[1])
    client = make_client(server)
    response = client._request("GET", "/invoices/ksef/X-1", headers={"X-Test": "1"})
    assert response.text == "<Faktura>1</Faktura>"
    assert server.ksef_calls == 2
    assert server.seen_headers[0].get("x-test") == "1"
    assert server.seen_headers[1].get("x-test") == "1"
