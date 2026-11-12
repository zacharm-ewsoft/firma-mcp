from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.redirect_validation import validate_redirect_uri

from firma_mcp.auth import (
    TOKEN_CLIENT_ID,
    SharedSecretVerifier,
    build_auth,
    build_guard,
    require_allowed_user,
    require_token_client,
)
from firma_mcp.config import DEFAULT_REDIRECT_URIS, CoreSettings

OWNER = frozenset({"owner"})


def token_with(login):
    return lambda: SimpleNamespace(claims={"login": login})


def test_allowed_login_passes():
    assert require_allowed_user(OWNER, token_getter=token_with("owner")) == "owner"


def test_login_comparison_ignores_case():
    assert require_allowed_user(OWNER, token_getter=token_with("Owner")) == "Owner"


def test_other_login_rejected():
    with pytest.raises(ToolError, match="Access denied"):
        require_allowed_user(OWNER, token_getter=token_with("intruder"))


def test_missing_token_rejected():
    with pytest.raises(ToolError, match="Access denied"):
        require_allowed_user(OWNER, token_getter=lambda: None)


def test_token_without_claims_rejected():
    with pytest.raises(ToolError, match="Access denied"):
        require_allowed_user(OWNER, token_getter=lambda: SimpleNamespace(claims=None))


def test_build_auth_github_mode(env):
    auth = build_auth(CoreSettings.from_env(env))
    assert type(auth).__name__ == "GitHubProvider"


async def test_shared_secret_verifier_accepts_only_the_exact_token():
    secret = "s" * 40
    verifier = SharedSecretVerifier(secret, "https://mcp.example.test")
    accepted = await verifier.verify_token(secret)
    assert accepted is not None and accepted.client_id == TOKEN_CLIENT_ID
    assert await verifier.verify_token(secret[:-1]) is None
    assert await verifier.verify_token("") is None
    assert await verifier.verify_token("żółć") is None  # non-ASCII input must not raise


def test_build_auth_token_mode(env):
    env.update(FIRMA_AUTH_MODE="token", FIRMA_AUTH_TOKEN="t" * 40)
    assert isinstance(build_auth(CoreSettings.from_env(env)), SharedSecretVerifier)


def test_token_guard_requires_the_static_client():
    assert require_token_client(token_getter=lambda: SimpleNamespace(client_id=TOKEN_CLIENT_ID))
    with pytest.raises(ToolError, match="Access denied"):
        require_token_client(token_getter=lambda: None)
    with pytest.raises(ToolError, match="Access denied"):
        require_token_client(token_getter=lambda: SimpleNamespace(client_id="someone-else"))


def test_guard_from_configuration_admits_only_listed_github_accounts(env):
    """FIRMA_ALLOWED_GITHUB_LOGINS end to end: configuration -> guard -> decision."""
    env["FIRMA_ALLOWED_GITHUB_LOGINS"] = "owner-login, bookkeeper"
    settings = CoreSettings.from_env(env)
    assert build_guard(settings, token_getter=token_with("owner-login"))() == "owner-login"
    assert build_guard(settings, token_getter=token_with("Bookkeeper"))() == "Bookkeeper"
    for outsider in ("someone-else", "owner-login-2", "", None):
        with pytest.raises(ToolError, match="Access denied"):
            build_guard(settings, token_getter=token_with(outsider))()
    with pytest.raises(ToolError, match="Access denied"):
        build_guard(settings, token_getter=lambda: None)()


def test_guard_in_token_mode_ignores_github_claims(env):
    env.update(FIRMA_AUTH_MODE="token", FIRMA_AUTH_TOKEN="t" * 40)
    guard = build_guard(CoreSettings.from_env(env), token_getter=token_with("owner-login"))
    with pytest.raises(ToolError, match="Access denied"):
        guard()


def test_github_sign_in_requests_only_read_access_to_the_profile(env):
    auth = build_auth(CoreSettings.from_env(env))
    assert auth.required_scopes == ["read:user"]


@pytest.mark.parametrize(
    "uri",
    [
        "https://claude.ai/api/mcp/auth_callback",
        "https://chatgpt.com/connector_platform_oauth_redirect",
        "https://chatgpt.com/connector/oauth/abc123",
        "http://localhost:6274/oauth/callback",
        "http://127.0.0.1:3118/callback",
    ],
)
def test_default_redirects_accept_claude_chatgpt_and_loopback(uri):
    assert validate_redirect_uri(uri, list(DEFAULT_REDIRECT_URIS)) is True


@pytest.mark.parametrize(
    "uri",
    [
        "https://evil.example/api/mcp/auth_callback",
        "https://claude.ai.evil.example/api/mcp/auth_callback",
        "https://chatgpt.com.evil.example/connector/oauth/abc",
        "http://localhost@evil.example/callback",
    ],
)
def test_default_redirects_reject_other_hosts(uri):
    assert validate_redirect_uri(uri, list(DEFAULT_REDIRECT_URIS)) is False
