"""Core settings read from environment variables.

Only the server itself is configured here: its public URL and how callers authenticate.
Each module reads its own variables (see ``firma_mcp.modules``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlparse

from firma_mcp.errors import ConfigError

AUTH_MODES = ("github", "token")
MIN_SECRET_LENGTH = 32

# Where each client sends the user back after the OAuth sign-in. Sources:
# https://claude.com/docs/connectors/building/authentication#callback-urls
# https://developers.openai.com/plugins/build/auth (section "Redirect URL")
DEFAULT_REDIRECT_URIS = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    "https://chatgpt.com/connector_platform_oauth_redirect",
    "https://chatgpt.com/connector/oauth/*",
    # Loopback redirects: Claude Code and MCP Inspector (RFC 8252, any port).
    "http://localhost:*",
    "http://127.0.0.1:*",
)


def split_csv(value: str | None) -> tuple[str, ...]:
    return tuple(item.strip() for item in (value or "").split(",") if item.strip())


def read_flag(env: Mapping[str, str], key: str, *, default: bool) -> bool:
    raw = (env.get(key) or "").strip().lower()
    if raw == "":
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{key} must be 'true' or 'false', got {raw!r}")


def read_group(env: Mapping[str, str], module: str, required: tuple[str, ...]) -> dict[str, str] | None:
    """Read one module's required variables as a group.

    Returns ``None`` when none of them is set (the module stays off) and raises when only
    some are set, because a half-configured module is almost always a mistake.
    """
    present = [key for key in required if env.get(key)]
    if not present:
        return None
    missing = [key for key in required if not env.get(key)]
    if missing:
        raise ConfigError(f"Module '{module}' is partly configured; missing: {', '.join(missing)}")
    return {key: env[key] for key in required}


@dataclass(frozen=True)
class CoreSettings:
    base_url: str
    auth_mode: str
    host: str
    port: int
    # Build the booking history in the background at start-up (see modules/booking.py).
    warm_history: bool = True
    # auth_mode == "github"
    github_client_id: str = ""
    github_client_secret: str = ""
    allowed_github_logins: frozenset[str] = frozenset()
    jwt_signing_key: str = ""
    allowed_redirect_uris: tuple[str, ...] = DEFAULT_REDIRECT_URIS
    # auth_mode == "token"
    auth_token: str = ""

    @property
    def public_host(self) -> str:
        return urlparse(self.base_url).netloc

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> CoreSettings:
        base_url = (env.get("FIRMA_BASE_URL") or "").rstrip("/")
        if not base_url:
            raise ConfigError("FIRMA_BASE_URL is required (the public https:// address of this server)")
        parsed = urlparse(base_url)
        is_local = parsed.hostname in ("localhost", "127.0.0.1")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and is_local):
            raise ConfigError("FIRMA_BASE_URL must use https:// (http:// is accepted for localhost only)")

        mode = (env.get("FIRMA_AUTH_MODE") or "github").strip().lower()
        if mode not in AUTH_MODES:
            raise ConfigError(f"FIRMA_AUTH_MODE must be one of: {', '.join(AUTH_MODES)}")

        try:
            port = int(env.get("FIRMA_PORT") or 8600)
        except ValueError:
            raise ConfigError("FIRMA_PORT must be a number") from None
        common = {
            "base_url": base_url,
            "auth_mode": mode,
            "host": env.get("FIRMA_HOST") or "127.0.0.1",
            "port": port,
            "warm_history": read_flag(env, "FIRMA_WARM_HISTORY", default=True),
        }

        if mode == "token":
            token = env.get("FIRMA_AUTH_TOKEN") or ""
            if len(token) < MIN_SECRET_LENGTH:
                raise ConfigError(
                    f"FIRMA_AUTH_TOKEN is required in token mode and must have at least "
                    f"{MIN_SECRET_LENGTH} characters (generate one with: openssl rand -hex 32)"
                )
            return cls(**common, auth_token=token)

        missing = [
            key
            for key in (
                "GITHUB_CLIENT_ID",
                "GITHUB_CLIENT_SECRET",
                "FIRMA_ALLOWED_GITHUB_LOGINS",
                "FIRMA_JWT_SIGNING_KEY",
            )
            if not env.get(key)
        ]
        if missing:
            raise ConfigError(
                "Authentication is not configured, refusing to start. Missing: " + ", ".join(missing)
            )
        logins = frozenset(login.lower() for login in split_csv(env["FIRMA_ALLOWED_GITHUB_LOGINS"]))
        if not logins:
            raise ConfigError("FIRMA_ALLOWED_GITHUB_LOGINS must list at least one GitHub login")
        if len(env["FIRMA_JWT_SIGNING_KEY"]) < MIN_SECRET_LENGTH:
            raise ConfigError(
                f"FIRMA_JWT_SIGNING_KEY must have at least {MIN_SECRET_LENGTH} characters "
                "(generate one with: openssl rand -hex 32)"
            )
        redirects = split_csv(env.get("FIRMA_ALLOWED_REDIRECT_URIS")) or DEFAULT_REDIRECT_URIS
        return cls(
            **common,
            github_client_id=env["GITHUB_CLIENT_ID"],
            github_client_secret=env["GITHUB_CLIENT_SECRET"],
            allowed_github_logins=logins,
            jwt_signing_key=env["FIRMA_JWT_SIGNING_KEY"],
            allowed_redirect_uris=redirects,
        )
