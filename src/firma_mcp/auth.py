"""Who may call the server.

Two modes, chosen with ``FIRMA_AUTH_MODE``:

* ``github`` (default): OAuth 2.1 sign-in through a GitHub OAuth app, proxied by FastMCP so
  that MCP clients get dynamic client registration and PKCE. After sign-in, every tool call
  checks the caller's GitHub login against an allowlist. This is what claude.ai and ChatGPT need.
* ``token``: one long shared secret sent as ``Authorization: Bearer <token>``. For clients that
  can set a request header (Claude Code, API integrations, MCP Inspector).

There is no unauthenticated mode.
"""

from __future__ import annotations

import hmac
from collections.abc import Callable

from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken, AuthProvider, TokenVerifier
from fastmcp.server.dependencies import get_access_token

from firma_mcp.config import CoreSettings

DENIED = "Access denied: this tool is available to the owner only."
TOKEN_CLIENT_ID = "static-token"
GITHUB_SCOPE = "read:user"


class SharedSecretVerifier(TokenVerifier):
    """Accepts exactly one bearer token, compared in constant time."""

    def __init__(self, secret: str, base_url: str):
        super().__init__(base_url=base_url)
        self._secret = secret.encode()

    async def verify_token(self, token: str) -> AccessToken | None:
        if not hmac.compare_digest(token.encode(), self._secret):
            return None
        return AccessToken(token=token, client_id=TOKEN_CLIENT_ID, scopes=[])


def build_auth(settings: CoreSettings) -> AuthProvider:
    if settings.auth_mode == "token":
        return SharedSecretVerifier(settings.auth_token, settings.base_url)

    from fastmcp.server.auth.providers.github import GitHubProvider

    return GitHubProvider(
        client_id=settings.github_client_id,
        client_secret=settings.github_client_secret,
        base_url=settings.base_url,
        jwt_signing_key=settings.jwt_signing_key,
        allowed_client_redirect_uris=list(settings.allowed_redirect_uris),
        # Read-only access to the public profile is enough to learn the login.
        required_scopes=[GITHUB_SCOPE],
    )


def require_allowed_user(
    allowed: frozenset[str], *, token_getter: Callable[[], object] = get_access_token
) -> str:
    """Per-call check for GitHub mode: the signed-in login must be on the allowlist.

    Signing in proves only that the caller has *a* GitHub account, so this check is what
    actually restricts the server to its owner.
    """
    token = token_getter()
    claims = getattr(token, "claims", None) or {}
    login = claims.get("login")
    if not login or login.lower() not in allowed:
        raise ToolError(DENIED)
    return login


def require_token_client(*, token_getter: Callable[[], object] = get_access_token) -> str:
    """Per-call check for token mode. The transport already rejected bad tokens; this is
    defence in depth so a tool never runs without an authenticated request."""
    token = token_getter()
    if getattr(token, "client_id", None) != TOKEN_CLIENT_ID:
        raise ToolError(DENIED)
    return TOKEN_CLIENT_ID


def build_guard(
    settings: CoreSettings, *, token_getter: Callable[[], object] = get_access_token
) -> Callable[[], str]:
    """The per-call access check for the configured mode."""
    if settings.auth_mode == "token":
        return lambda: require_token_client(token_getter=token_getter)
    return lambda: require_allowed_user(settings.allowed_github_logins, token_getter=token_getter)
