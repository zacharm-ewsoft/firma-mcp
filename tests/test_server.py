"""End-to-end tests of the assembled server, called in-process through the MCP client.

Every external system is replaced by a fake; no test here opens a network connection.
"""

import asyncio
import datetime as dt
import logging
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from firma_mcp.errors import ConfigError
from firma_mcp.modules.merit import suggestions
from firma_mcp.server import Settings, build_instructions, create_server

ALL_TOOLS = {
    "mail_list",
    "mail_get",
    "mail_attachment",
    "merit_purchases",
    "merit_sales",
    "merit_invoice",
    "merit_payments",
    "payment_check",
    "ksef_received",
    "ksef_invoice",
    "booking_suggestions",
    "fakturownia_sales",
    "fakturownia_purchases",
    "fakturownia_invoice",
    "fakturownia_payment_status",
}
# The tools the original production server exposes. Their names must never change.
PRODUCTION_TOOLS = ALL_TOOLS - {t for t in ALL_TOOLS if t.startswith("fakturownia_")}


@pytest.fixture(autouse=True)
def _clear_suggestions_cache():
    suggestions.clear_cache()
    yield
    suggestions.clear_cache()


def fake_msg(uid):
    attachment = SimpleNamespace(
        filename="note.txt", payload=b"attachment text", content_type="text/plain", size=15
    )
    return SimpleNamespace(
        uid=uid,
        subject=f"S{uid}",
        from_="a@b.test",
        to=("office@example.com",),
        date=dt.datetime(2026, 9, 9, tzinfo=dt.UTC),
        text="body",
        html="",
        flags=(),
        attachments=[attachment],
    )


class FakeBox:
    def fetch(self, criteria="ALL", *, mark_seen=True, reverse=False, limit=None, uid_list=None, bulk=False):
        msgs = [fake_msg("1"), fake_msg("2")]
        if uid_list:
            msgs = [m for m in msgs if m.uid in uid_list]
        return iter(msgs)


@contextmanager
def fake_mailbox(config, box):
    yield FakeBox()


class FakeMerit:
    """Stands in for the merit-activa-api client (the raw library object)."""

    def get_purchase_invoices(self, start, end, unpaid=False):
        return [
            {
                "PIHId": "p-1",
                "BillNo": "FV 12/2026",
                "VendorName": "Hosting Example",
                "VatRegNo": "2222222222",
                "VendorRegNo": "222222222",
                "TotalSum": 10,
                "Paid": False,
            }
        ]

    def get_invoices(self, start, end, unpaid=False, department_code=None):
        return [{"SIHId": "s-1", "InvoiceNo": "1/09/2026", "CustomerName": "K", "TotalSum": 5, "Paid": True}]

    def get_payments(self, start, end):
        return [
            {
                "PHId": "ph-1",
                "DocumentDate": "2026-09-05T00:00:00",
                "BankName": "Example Bank",
                "CounterPartName": "Hosting Example",
                "CounterPartType": "Vendor",
                "Direction": 2,
                "Amount": 10,
                "CurrencyCode": "PLN",
                "DocumentNo": "WB 1",
            }
        ]

    def _request(self, endpoint, payload=None, version="v1", params=None):
        if endpoint == "getpurchorder":
            return {"Header": {"PIHId": payload["Id"]}, "Attachment": {"Filename": "a.pdf"}}
        if endpoint == "getinvoice":
            return {"Header": {"SIHId": payload["Id"]}}
        raise AssertionError(f"unexpected endpoint {endpoint!r}")


class FakeKsef:
    def received(self, days):
        return [
            {"ksef_number": "K-1", "invoice_number": "FV12/2026", "seller_nip": "2222222222"},
            {"ksef_number": "K-2", "invoice_number": "FV99/2026", "seller_nip": "3333333333"},
        ]

    def invoice_xml(self, ksef_number):
        return f"<Faktura>{ksef_number}</Faktura>"


class BrokenKsef:
    def received(self, days):
        raise RuntimeError("KSeF down")


class BrokenMerit:
    def get_purchase_invoices(self, start, end, unpaid=False):
        raise RuntimeError("Merit down")


class FakeFakturownia:
    def sales(self, days, unpaid_only=False):
        return [{"id": 1, "number": "FS 1/2026", "paid": False}]

    def costs(self, days, unpaid_only=False):
        return [{"id": 2, "number": "FK 7/2026", "paid": True}]

    def invoice(self, invoice_id, with_pdf=False):
        return {"id": int(invoice_id), "positions": [], "raw": {}}

    def payment_status(self, invoice_id):
        return {"id": int(invoice_id), "paid": False, "overdue": True}


def make_server(env, **overrides):
    """The server with every external system faked. Authentication is bypassed with an
    explicit guard, which only tests are allowed to do."""
    fakes = {"merit": FakeMerit(), "ksef": FakeKsef(), "mail": fake_mailbox, "fakturownia": FakeFakturownia()}
    background = overrides.pop("background", False)
    guard = overrides.pop("guard", lambda: "tester")
    return create_server(
        Settings.from_env(env),
        auth=None,
        guard=guard,
        overrides={**fakes, **overrides},
        background=background,
    )


@pytest.fixture
def server(env):
    return make_server(env)


async def call(server, name, **args):
    async with Client(server) as client:
        result = await client.call_tool(name, args)
        return result.data


async def tool_names(server) -> set[str]:
    async with Client(server) as client:
        return {t.name for t in await client.list_tools()}


def without(env, *prefixes):
    return {k: v for k, v in env.items() if not k.startswith(prefixes)}


# -- registration ------------------------------------------------------------------


async def test_all_tools_are_registered(server):
    assert await tool_names(server) == ALL_TOOLS


async def test_production_tool_names_and_parameters_are_stable(server):
    """The daily production client depends on these exact names and parameters."""
    expected = {
        "mail_list": {"box", "days", "limit"},
        "mail_get": {"box", "uid"},
        "mail_attachment": {"box", "uid", "filename", "mode"},
        "merit_purchases": {"days", "unpaid_only"},
        "merit_sales": {"days", "unpaid_only"},
        "merit_invoice": {"kind", "id", "with_pdf"},
        "merit_payments": {"days"},
        "payment_check": {"amount", "counterparty", "nip", "days"},
        "booking_suggestions": {"days"},
        "ksef_received": {"days"},
        "ksef_invoice": {"ksef_number"},
    }
    assert set(expected) == PRODUCTION_TOOLS
    async with Client(server) as client:
        tools = {t.name: t for t in await client.list_tools()}
    for name, params in expected.items():
        assert set(tools[name].input_schema["properties"]) == params, name


async def test_every_tool_is_annotated_read_only(server):
    async with Client(server) as client:
        tools = await client.list_tools()
    for tool in tools:
        assert tool.annotations is not None, tool.name
        assert tool.annotations.read_only_hint is True, tool.name
        assert tool.annotations.destructive_hint is False, tool.name


async def test_module_without_configuration_is_not_registered(env):
    names = await tool_names(make_server(without(env, "FAKTUROWNIA_", "MAIL_")))
    assert names == PRODUCTION_TOOLS - {"mail_list", "mail_get", "mail_attachment"}


async def test_booking_suggestions_needs_both_ksef_and_merit(env):
    assert "booking_suggestions" not in await tool_names(make_server(without(env, "MERIT_")))
    assert "booking_suggestions" not in await tool_names(make_server(without(env, "KSEF_")))


def test_no_module_configured_refuses_to_start(env):
    core_only = without(env, "MAIL_", "MERIT_", "KSEF_", "FAKTUROWNIA_")
    with pytest.raises(ConfigError, match="No module is configured"):
        Settings.from_env(core_only)


def test_partly_configured_module_refuses_to_start(env):
    del env["MERIT_API_KEY"]
    with pytest.raises(ConfigError, match="MERIT_API_KEY"):
        Settings.from_env(env)


def test_instructions_list_only_enabled_modules(env):
    text = build_instructions(Settings.from_env(without(env, "FAKTUROWNIA_")).modules)
    assert "ksef_received" in text and "Fakturownia" not in text


def test_server_builds_authentication_by_default(env):
    """Unless a test opts out explicitly, the server always carries an auth provider."""
    server = create_server(Settings.from_env(env), overrides={"merit": FakeMerit(), "ksef": FakeKsef()})
    assert server.auth is not None


# -- mail --------------------------------------------------------------------------


async def test_mail_list_and_get(server):
    out = await call(server, "mail_list", box="office@example.com", days=3)
    assert out["count"] == 2 and out["items"][0]["subject"] == "S1"
    one = await call(server, "mail_get", box="office@example.com", uid="2")
    assert one["subject"] == "S2" and one["text"] == "body"


async def test_mail_list_limit_and_truncated(server):
    full = await call(server, "mail_list", box="office@example.com", days=3, limit=2)
    assert full["count"] == 2 and full["truncated"] is True
    partial = await call(server, "mail_list", box="office@example.com", days=3, limit=40)
    assert partial["count"] == 2 and partial["truncated"] is False


async def test_mail_attachment_reads_text(server):
    out = await call(server, "mail_attachment", box="office@example.com", uid="1", filename="note.txt")
    assert out["text"] == "attachment text"
    assert out["mode"] == "text"


async def test_mail_list_unknown_box_returns_error_dict(server):
    out = await call(server, "mail_list", box="x@y.test", days=3)
    assert out["source"] == "mail" and "unknown mailbox" in out["error"]


# -- merit -------------------------------------------------------------------------


async def test_merit_tools(server):
    p = await call(server, "merit_purchases", days=7, unpaid_only=True)
    assert p["count"] == 1 and p["items"][0]["vendor"] == "Hosting Example"
    s = await call(server, "merit_sales", days=7)
    assert s["items"][0]["customer"] == "K"
    inv = await call(server, "merit_invoice", kind="purchase", id="p-1")
    assert "Attachment" not in inv


async def test_merit_payments(server):
    out = await call(server, "merit_payments", days=31)
    assert out["count"] == 1 and out["items"][0]["counterparty"] == "Hosting Example"


async def test_payment_check_found(server):
    out = await call(server, "payment_check", amount="10.00", days=45)
    assert out["found"] is True and out["checked_days"] == 45


# -- ksef and cross-module behaviour -----------------------------------------------


async def test_ksef_received_marks_booked(server):
    out = await call(server, "ksef_received", days=7)
    by_number = {i["ksef_number"]: i for i in out["items"]}
    assert by_number["K-1"]["booked_in_merit"] is True and by_number["K-1"]["merit_id"] == "p-1"
    assert by_number["K-2"]["booked_in_merit"] is False


async def test_ksef_received_without_merit_module_has_no_booked_flag(env):
    out = await call(make_server(without(env, "MERIT_")), "ksef_received", days=7)
    assert out["count"] == 2
    assert all("booked_in_merit" not in item for item in out["items"])


async def test_ksef_error_isolated(env):
    server = make_server(env, ksef=BrokenKsef())
    out = await call(server, "ksef_received", days=7)
    assert out["source"] == "ksef" and "KSeF down" in out["error"]
    ok = await call(server, "merit_purchases", days=7)
    assert ok["count"] == 1


async def test_ksef_received_when_merit_fails_still_returns_invoices(env):
    out = await call(make_server(env, merit=BrokenMerit()), "ksef_received", days=7)
    assert "error" not in out
    assert out["count"] == 2
    for item in out["items"]:
        assert item["booked_in_merit"] is False
        assert item["merit_id"] is None


async def test_booking_suggestions_only_unbooked(server):
    out = await call(server, "booking_suggestions", days=7)
    assert out["count"] == 1
    item = out["items"][0]
    assert item["seller_nip"] == "3333333333"  # K-2 is the only unbooked invoice in FakeKsef
    assert item["account"] is None
    assert item["basis"] == 0
    assert item["note"] == suggestions.NO_HISTORY_NOTE
    assert out["history_truncated"] is False
    assert out["history_invoices"] == 1  # FakeMerit has a single purchase invoice
    assert out["history_partial"] is False


async def test_booking_suggestions_merit_error_surfaces(env):
    out = await call(make_server(env, merit=BrokenMerit()), "booking_suggestions", days=7)
    assert out["source"] == "merit"
    assert "error" in out


async def test_ksef_invoice_xml(server):
    out = await call(server, "ksef_invoice", ksef_number="K-1")
    assert out["xml"] == "<Faktura>K-1</Faktura>" and out["truncated"] is False


async def test_days_are_clamped(env):
    class SpyKsef(FakeKsef):
        seen = None

        def received(self, days):
            SpyKsef.seen = days
            return []

    await call(make_server(env, ksef=SpyKsef()), "ksef_received", days=999)
    assert SpyKsef.seen == 31


# -- fakturownia (experimental) ----------------------------------------------------


async def test_fakturownia_tools(server):
    assert (await call(server, "fakturownia_sales", days=7))["items"][0]["number"] == "FS 1/2026"
    assert (await call(server, "fakturownia_purchases", days=7))["items"][0]["number"] == "FK 7/2026"
    assert (await call(server, "fakturownia_invoice", id="2"))["id"] == 2
    assert (await call(server, "fakturownia_payment_status", id="1"))["overdue"] is True


async def test_fakturownia_tools_are_labelled_experimental(server):
    async with Client(server) as client:
        tools = await client.list_tools()
    for tool in tools:
        if tool.name.startswith("fakturownia_"):
            assert tool.description.startswith("EXPERIMENTAL"), tool.name


# -- access control and logging ----------------------------------------------------


async def test_successful_call_logs_caller_and_tool_without_data(server, caplog):
    with caplog.at_level(logging.INFO, logger="firma_mcp"):
        await call(server, "merit_purchases", days=7, unpaid_only=True)
    messages = [r.getMessage() for r in caplog.records if r.name == "firma_mcp"]
    assert any("tool=merit_purchases" in m and "caller=tester" in m for m in messages)
    for message in messages:
        assert "Hosting Example" not in message
        assert "FV 12/2026" not in message


async def test_guard_blocks(env):
    def deny():
        raise ToolError("Access denied")

    async with Client(make_server(env, guard=deny)) as client:
        with pytest.raises(Exception, match="Access denied"):
            await client.call_tool("merit_purchases", {"days": 3})


async def test_default_guard_denies_a_call_without_a_token(env):
    """With the real guard and no authenticated request, a tool must not run."""
    server = create_server(
        Settings.from_env(env), auth=None, overrides={"merit": FakeMerit(), "ksef": FakeKsef()}
    )
    async with Client(server) as client:
        with pytest.raises(Exception, match="Access denied"):
            await client.call_tool("merit_purchases", {"days": 3})


async def test_signed_in_account_outside_the_allowlist_is_rejected(env):
    """A real sign-in by a GitHub account that is not listed must not reach any tool."""
    from types import SimpleNamespace as Token

    from firma_mcp.auth import build_guard

    settings = Settings.from_env(env)
    outsider = build_guard(settings.core, token_getter=lambda: Token(claims={"login": "not-on-the-list"}))
    owner = build_guard(settings.core, token_getter=lambda: Token(claims={"login": "owner-login"}))
    fakes = {"merit": FakeMerit(), "ksef": FakeKsef()}

    async with Client(create_server(settings, auth=None, guard=outsider, overrides=fakes)) as client:
        with pytest.raises(Exception, match="Access denied"):
            await client.call_tool("merit_purchases", {"days": 3})
    allowed = await call(create_server(settings, auth=None, guard=owner, overrides=fakes), "merit_purchases")
    assert allowed["count"] == 1


# -- background history warm-up ----------------------------------------------------


async def test_warm_up_is_off_by_default(env):
    make_server(env)
    await asyncio.sleep(0.05)
    assert suggestions._cache == {}


async def test_warm_up_builds_the_history_cache_shortly_after_start(env):
    calls = {"n": 0}

    class SpyMerit(FakeMerit):
        def get_purchase_invoices(self, start, end, unpaid=False):
            calls["n"] += 1
            return super().get_purchase_invoices(start, end, unpaid)

    make_server(env, merit=SpyMerit(), background=True)

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not suggestions._cache:
        await asyncio.sleep(0.01)
    assert calls["n"] > 0, "the warm-up thread should call history() within 2 s of start"
    assert suggestions._cache

    # The refresh interval is measured in hours, so within this short window the loop must
    # not call history() a second time.
    calls_after_warmup = calls["n"]
    await asyncio.sleep(0.2)
    assert calls["n"] == calls_after_warmup
