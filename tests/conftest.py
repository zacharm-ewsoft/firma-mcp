import pytest


@pytest.fixture
def env() -> dict[str, str]:
    """A complete, entirely made-up configuration with every module enabled."""
    return {
        "FIRMA_BASE_URL": "https://mcp.example.test",
        "GITHUB_CLIENT_ID": "gh-id",
        "GITHUB_CLIENT_SECRET": "gh-secret",
        "FIRMA_ALLOWED_GITHUB_LOGINS": "Owner-Login, other-login",
        "FIRMA_JWT_SIGNING_KEY": "x" * 64,
        "MAIL_HOST": "mail.example.test",
        "MAIL_BOXES": "owner@example.com,office@example.com",
        "MAIL_PASSWORD__owner_example_com": "pw-owner",
        "MAIL_PASSWORD__office_example_com": "pw-office",
        "MERIT_API_ID": "merit-id",
        "MERIT_API_KEY": "merit-key",
        "KSEF_TOKEN": "ksef-token",
        "KSEF_NIP": "1111111111",
        "KSEF_ENV": "test",
        "FAKTUROWNIA_DOMAIN": "acme",
        "FAKTUROWNIA_API_TOKEN": "fakturownia-token",
    }
