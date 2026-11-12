"""Builds the MCP server: a shared skeleton plus the modules that are configured."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from fastmcp import FastMCP

from firma_mcp import __version__
from firma_mcp.auth import build_auth, build_guard
from firma_mcp.config import CoreSettings
from firma_mcp.errors import ConfigError
from firma_mcp.modules import Module, load_enabled
from firma_mcp.toolkit import Context

log = logging.getLogger("firma_mcp")

SERVER_NAME = "firma-mcp"
INSTRUCTIONS_HEAD = (
    "Read-only access to one company's back-office systems. Nothing here can send, create, "
    "change or delete anything. Amounts are decimal strings with a dot (e.g. '123.45'). "
    "Start with the list tools and ask for details one item at a time. A result with an "
    "`error` field means that one system failed; the others can still be used."
)
_FROM_SETTINGS = object()


@dataclass(frozen=True)
class Settings:
    core: CoreSettings
    modules: tuple[tuple[Module, Any], ...]

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        core = CoreSettings.from_env(env)
        modules = tuple(load_enabled(env))
        if not modules:
            raise ConfigError(
                "No module is configured, so there is nothing to serve. "
                "Configure at least one of: mail, merit, ksef, fakturownia (see .env.example)."
            )
        return cls(core=core, modules=modules)

    @property
    def module_names(self) -> tuple[str, ...]:
        return tuple(module.name for module, _ in self.modules)


def build_instructions(modules: tuple[tuple[Module, Any], ...]) -> str:
    return "\n".join([INSTRUCTIONS_HEAD, *(f"- {module.summary}" for module, _ in modules)])


def create_server(
    settings: Settings,
    *,
    auth: Any = _FROM_SETTINGS,
    guard: Callable[[], object] | None = None,
    overrides: dict[str, Any] | None = None,
    background: bool = False,
) -> FastMCP:
    """Create the server for ``settings``.

    Authentication is built from the settings unless a caller passes ``auth`` explicitly.
    Only tests do that, to call tools in-process; the entry point never does.
    """
    if auth is _FROM_SETTINGS:
        auth = build_auth(settings.core)
    ctx = Context(
        guard=guard or build_guard(settings.core),
        overrides=overrides or {},
        background=background,
    )
    mcp = FastMCP(
        name=SERVER_NAME,
        version=__version__,
        instructions=build_instructions(settings.modules),
        auth=auth,
    )
    for module, config in settings.modules:
        module.register(mcp, config, ctx)
        log.info("module enabled: %s (%s)", module.name, module.status)
    return mcp
