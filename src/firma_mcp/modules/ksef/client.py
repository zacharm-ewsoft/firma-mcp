"""KSeF 2.0 client: sign in with a KSeF token, read metadata and XML of received invoices."""

from __future__ import annotations

import base64
import datetime as dt
import time
from collections.abc import Callable
from decimal import Decimal, InvalidOperation

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from firma_mcp.errors import SourceError

SOURCE = "ksef"
BASE_URLS = {
    "prod": "https://api.ksef.mf.gov.pl/v2",
    "test": "https://api-test.ksef.mf.gov.pl/v2",
}
MAX_RANGE_DAYS = 100  # API limit for dateRange
PAGE_SIZE = 100
POLL_ATTEMPTS = 20
MAX_PAGES = 200  # hard stop for one received() call, so a paging bug cannot loop forever


def encrypt_token(token: str, timestamp_ms: int, cert_der_b64: str) -> str:
    """Encrypt ``token|timestamp`` with the Ministry's public key (RSA-OAEP, SHA-256)."""
    cert = x509.load_der_x509_certificate(base64.b64decode(cert_der_b64))
    public_key = cert.public_key()
    payload = f"{token}|{timestamp_ms}".encode()
    blob = public_key.encrypt(
        payload,
        padding.OAEP(mgf=padding.MGF1(algorithm=hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )
    return base64.b64encode(blob).decode()


def _money(value) -> str | None:
    if value is None:
        return None
    try:
        return f"{Decimal(str(value)).quantize(Decimal('0.01'))}"
    except InvalidOperation:
        return str(value)


def _expect_dict(data, what: str) -> dict:
    """A response body must be a JSON object; anything else is reported as a source error."""
    if not isinstance(data, dict):
        preview = str(data)[:100]
        raise SourceError(
            SOURCE,
            f"Unexpected KSeF response for {what}: expected an object, "
            f"got {type(data).__name__} ({preview!r})",
        )
    return data


def map_invoice(raw: dict) -> dict:
    seller = raw.get("seller") or {}
    return {
        "ksef_number": raw.get("ksefNumber"),
        "invoice_number": raw.get("invoiceNumber"),
        "seller_nip": seller.get("nip"),
        "seller_name": seller.get("name"),
        "issue_date": raw.get("issueDate"),
        "ksef_date": raw.get("permanentStorageDate") or raw.get("invoicingDate"),
        "gross": _money(raw.get("grossAmount")),
        "net": _money(raw.get("netAmount")),
        "vat": _money(raw.get("vatAmount")),
        "currency": raw.get("currency"),
        "invoice_type": raw.get("invoiceType"),
        "has_attachment": bool(raw.get("hasAttachment")),
    }


class KsefClient:
    def __init__(
        self,
        token: str,
        nip: str,
        env: str = "prod",
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 30,
    ):
        if env not in BASE_URLS:
            raise SourceError(SOURCE, f"unknown KSEF_ENV={env}")
        self._token = token
        self._nip = nip
        self._sleep = sleep
        self._http = httpx.Client(
            base_url=BASE_URLS[env],
            timeout=timeout,
            transport=transport,
            headers={"X-Error-Format": "problem-details"},
        )
        self._access_token: str | None = None

    # -- authentication ---------------------------------------------------
    def _raise_for(self, response: httpx.Response, what: str) -> None:
        if response.status_code >= 400:
            detail = response.text[:300]
            raise SourceError(SOURCE, f"{what}: HTTP {response.status_code} {detail}")

    def _encryption_cert(self) -> tuple[str, str]:
        response = self._http.get("/security/public-key-certificates")
        self._raise_for(response, "fetching the KSeF certificate")
        for cert in response.json():
            if "KsefTokenEncryption" in cert.get("usage", []):
                return cert["certificate"], cert["publicKeyId"]
        raise SourceError(SOURCE, "KSeF returned no certificate for token encryption")

    def authenticate(self) -> None:
        try:
            challenge_resp = self._http.post("/auth/challenge")
            self._raise_for(challenge_resp, "challenge")
            challenge = challenge_resp.json()
            cert_b64, key_id = self._encryption_cert()
            body = {
                "challenge": challenge["challenge"],
                "contextIdentifier": {"type": "Nip", "value": self._nip},
                "encryptedToken": encrypt_token(self._token, int(challenge["timestampMs"]), cert_b64),
                "publicKeyId": key_id,
            }
            init = self._http.post("/auth/ksef-token", json=body)
            self._raise_for(init, "starting authentication")
            data = init.json()
            ref = data["referenceNumber"]
            auth_token = data["authenticationToken"]["token"]
            headers = {"Authorization": f"Bearer {auth_token}"}
            for _ in range(POLL_ATTEMPTS):
                status_resp = self._http.get(f"/auth/{ref}", headers=headers)
                self._raise_for(status_resp, "authentication status")
                code = int(status_resp.json().get("status", {}).get("code", 0))
                if code == 200:
                    break
                if code != 100:
                    raise SourceError(SOURCE, f"authentication rejected (status {code})")
                self._sleep(1.0)
            else:
                raise SourceError(SOURCE, "authentication timed out")
            redeem = self._http.post("/auth/token/redeem", headers=headers)
            self._raise_for(redeem, "redeeming the access token")
            self._access_token = redeem.json()["accessToken"]["token"]
        except httpx.HTTPError as exc:
            raise SourceError(SOURCE, f"connection to KSeF failed: {exc}") from exc
        except (KeyError, ValueError) as exc:
            raise SourceError(SOURCE, f"unexpected KSeF response during authentication: {exc}") from exc

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        if self._access_token is None:
            self.authenticate()
        extra_headers = dict(kwargs.pop("headers", None) or {})
        for attempt in (1, 2):
            headers = {**extra_headers, "Authorization": f"Bearer {self._access_token}"}
            try:
                response = self._http.request(method, path, headers=headers, **kwargs)
            except httpx.HTTPError as exc:
                raise SourceError(SOURCE, f"connection to KSeF failed: {exc}") from exc
            if response.status_code == 401 and attempt == 1:
                self.authenticate()
                continue
            self._raise_for(response, f"{method} {path}")
            return response
        raise SourceError(SOURCE, "KSeF: failed after re-authentication")  # pragma: no cover

    # -- reads --------------------------------------------------------------
    def received(self, days: int) -> list[dict]:
        """Metadata of invoices where this company is the buyer, stored in the last ``days`` days."""
        days = min(int(days), MAX_RANGE_DAYS)
        now = dt.datetime.now(dt.UTC)
        body = {
            "subjectType": "Subject2",
            "dateRange": {
                "dateType": "PermanentStorage",
                "from": (now - dt.timedelta(days=days)).isoformat(timespec="seconds"),
                "to": now.isoformat(timespec="seconds"),
            },
        }
        out: list[dict] = []
        seen: set[str] = set()
        offset = 0
        for _ in range(MAX_PAGES):
            response = self._request(
                "POST",
                "/invoices/query/metadata",
                json=body,
                params={"pageOffset": offset, "pageSize": PAGE_SIZE, "sortOrder": "Asc"},
            )
            try:
                data = response.json()
            except ValueError as exc:
                raise SourceError(
                    SOURCE, f"unexpected KSeF response for /invoices/query/metadata: {exc}"
                ) from exc
            data = _expect_dict(data, "/invoices/query/metadata")
            invoices = [i for i in (data.get("invoices") or []) if isinstance(i, dict)]
            for raw in invoices:
                number = raw.get("ksefNumber")
                if number is not None and number in seen:
                    continue  # duplicate, e.g. the boundary record repeated after isTruncated
                if number is not None:
                    seen.add(number)
                try:
                    out.append(map_invoice(raw))
                except Exception as exc:  # noqa: BLE001 - any mapping failure is a source error
                    raise SourceError(
                        SOURCE, f"unexpected invoice shape from KSeF ({type(exc).__name__}): {exc}"
                    ) from exc
            if not data.get("hasMore"):
                return out
            if data.get("isTruncated") and invoices:
                # The result set was cut off: restart paging just after the last record seen.
                last_date = invoices[-1].get("permanentStorageDate")
                if not last_date:
                    raise SourceError(
                        SOURCE, "KSeF: page truncated but the last record has no permanentStorageDate"
                    )
                try:
                    parsed = dt.datetime.fromisoformat(str(last_date))
                except ValueError as exc:
                    raise SourceError(
                        SOURCE, f"KSeF: cannot parse permanentStorageDate {last_date!r}: {exc}"
                    ) from exc
                next_from = parsed + dt.timedelta(milliseconds=1)
                body["dateRange"]["from"] = next_from.isoformat(timespec="milliseconds")
                offset = 0
            else:
                offset += 1
        raise SourceError(SOURCE, f"KSeF: page limit ({MAX_PAGES}) exceeded while listing invoices")

    def invoice_xml(self, ksef_number: str) -> str:
        response = self._request("GET", f"/invoices/ksef/{ksef_number}")
        return response.text
