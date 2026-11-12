from firma_mcp.matching import mark_booked, normalize_nip, normalize_number


def test_normalize_number():
    assert normalize_number(" fv 12/2026 ") == "FV12/2026"
    assert normalize_number("FV\\12\\2026") == "FV12/2026"
    assert normalize_number(None) == ""


def test_normalize_nip():
    assert normalize_nip("PL 111-111-11-11") == "1111111111"
    assert normalize_nip(None) == ""


def test_mark_booked_matches_on_nip_and_number():
    ksef = [
        {"ksef_number": "A", "invoice_number": "FV 12/2026", "seller_nip": "2222222222"},
        {"ksef_number": "B", "invoice_number": "FV 13/2026", "seller_nip": "2222222222"},
    ]
    merit = [{"id": "p-1", "number": "fv12/2026", "vendor_nip": "222-222-22-22"}]
    out = mark_booked(ksef, merit)
    assert out[0]["booked_in_merit"] is True and out[0]["merit_id"] == "p-1"
    assert out[1]["booked_in_merit"] is False and out[1]["merit_id"] is None
    assert "booked_in_merit" not in ksef[0]  # the input is left untouched
