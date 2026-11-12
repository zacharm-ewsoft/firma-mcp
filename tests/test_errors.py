from firma_mcp.errors import SourceError, clamp_days


def test_source_error_to_dict():
    err = SourceError("mail", "IMAP nie odpowiada")
    assert err.to_dict() == {"error": "IMAP nie odpowiada", "source": "mail"}
    assert str(err) == "[mail] IMAP nie odpowiada"


def test_clamp_days():
    assert clamp_days(None, default=3) == 3
    assert clamp_days(0, default=3) == 1
    assert clamp_days(-5, default=3) == 1
    assert clamp_days(7, default=3) == 7
    assert clamp_days(400, default=3) == 31
    assert clamp_days("12", default=3) == 12  # type: ignore[arg-type]
    assert clamp_days("abc", default=3) == 3  # type: ignore[arg-type]
