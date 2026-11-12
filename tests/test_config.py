import pytest

import firma_mcp
from firma_mcp.config import DEFAULT_REDIRECT_URIS, CoreSettings, read_group
from firma_mcp.errors import ConfigError


def test_package_importable():
    assert firma_mcp.__doc__ and firma_mcp.__version__


def test_github_mode_is_the_default(env):
    core = CoreSettings.from_env(env)
    assert core.auth_mode == "github"
    assert core.base_url == "https://mcp.example.test"
    assert core.public_host == "mcp.example.test"
    assert (core.host, core.port) == ("127.0.0.1", 8600)


def test_github_logins_are_parsed_and_lowercased(env):
    assert CoreSettings.from_env(env).allowed_github_logins == frozenset({"owner-login", "other-login"})


@pytest.mark.parametrize(
    "missing",
    ["GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET", "FIRMA_ALLOWED_GITHUB_LOGINS", "FIRMA_JWT_SIGNING_KEY"],
)
def test_refuses_to_start_without_complete_authentication(env, missing):
    del env[missing]
    with pytest.raises(ConfigError, match=missing):
        CoreSettings.from_env(env)


def test_refuses_an_empty_login_allowlist(env):
    env["FIRMA_ALLOWED_GITHUB_LOGINS"] = " , "
    with pytest.raises(ConfigError, match="at least one GitHub login"):
        CoreSettings.from_env(env)


def test_refuses_a_short_signing_key(env):
    env["FIRMA_JWT_SIGNING_KEY"] = "too-short"
    with pytest.raises(ConfigError, match="FIRMA_JWT_SIGNING_KEY"):
        CoreSettings.from_env(env)


def test_base_url_is_required_and_must_be_https(env):
    env["FIRMA_BASE_URL"] = "http://mcp.example.test"
    with pytest.raises(ConfigError, match="https"):
        CoreSettings.from_env(env)
    del env["FIRMA_BASE_URL"]
    with pytest.raises(ConfigError, match="FIRMA_BASE_URL"):
        CoreSettings.from_env(env)


def test_http_is_accepted_for_localhost_only(env):
    env["FIRMA_BASE_URL"] = "http://localhost:8600/"
    assert CoreSettings.from_env(env).base_url == "http://localhost:8600"


def test_unknown_auth_mode_is_rejected(env):
    env["FIRMA_AUTH_MODE"] = "none"
    with pytest.raises(ConfigError, match="FIRMA_AUTH_MODE"):
        CoreSettings.from_env(env)


def test_token_mode_requires_a_long_token(env):
    env["FIRMA_AUTH_MODE"] = "token"
    with pytest.raises(ConfigError, match="FIRMA_AUTH_TOKEN"):
        CoreSettings.from_env(env)
    env["FIRMA_AUTH_TOKEN"] = "short"
    with pytest.raises(ConfigError, match="at least 32"):
        CoreSettings.from_env(env)
    env["FIRMA_AUTH_TOKEN"] = "t" * 32
    assert CoreSettings.from_env(env).auth_mode == "token"


def test_redirect_uris_default_to_claude_and_chatgpt(env):
    uris = CoreSettings.from_env(env).allowed_redirect_uris
    assert uris == DEFAULT_REDIRECT_URIS
    assert "https://claude.ai/api/mcp/auth_callback" in uris
    assert "https://chatgpt.com/connector_platform_oauth_redirect" in uris


def test_redirect_uris_can_be_overridden(env):
    env["FIRMA_ALLOWED_REDIRECT_URIS"] = "https://claude.ai/api/mcp/auth_callback"
    assert CoreSettings.from_env(env).allowed_redirect_uris == ("https://claude.ai/api/mcp/auth_callback",)


def test_history_warm_up_is_on_by_default_and_can_be_switched_off(env):
    assert CoreSettings.from_env(env).warm_history is True
    env["FIRMA_WARM_HISTORY"] = "false"
    assert CoreSettings.from_env(env).warm_history is False
    env["FIRMA_WARM_HISTORY"] = "sometimes"
    with pytest.raises(ConfigError, match="FIRMA_WARM_HISTORY"):
        CoreSettings.from_env(env)


def test_read_group_all_none_or_error():
    keys = ("A", "B")
    assert read_group({}, "x", keys) is None
    assert read_group({"A": "1", "B": "2"}, "x", keys) == {"A": "1", "B": "2"}
    with pytest.raises(ConfigError, match="missing: B"):
        read_group({"A": "1"}, "x", keys)
