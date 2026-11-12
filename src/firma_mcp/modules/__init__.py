"""Modules: one per external system.

A module is two functions and a little metadata:

* ``load(env)`` reads the module's environment variables. It returns a config object when the
  module is fully configured, ``None`` when it is not configured at all, and raises
  ``ConfigError`` when it is only partly configured.
* ``register(mcp, config, ctx)`` adds the module's tools to the server.

The server enables exactly the modules whose ``load`` returned a config. See
``docs/adding-a-module.md`` for a walkthrough.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from fastmcp import FastMCP

from firma_mcp.toolkit import Context

PRODUCTION = "production"
EXPERIMENTAL = "experimental"


@dataclass(frozen=True)
class Module:
    name: str
    status: str
    summary: str  # one sentence for the server instructions shown to the model
    register: Callable[[FastMCP, Any, Context], None]
    load: Callable[[Mapping[str, str]], Any | None] | None = None
    # Names of modules that must be enabled for this one to be enabled. A module with
    # `requires` and no `load` is switched on purely by its dependencies.
    requires: tuple[str, ...] = ()


def all_modules() -> tuple[Module, ...]:
    """Registration order matters: a module can only use services of those before it."""
    from firma_mcp.modules import booking, fakturownia, ksef, mail, merit

    return (mail.MODULE, merit.MODULE, fakturownia.MODULE, ksef.MODULE, booking.MODULE)


def load_enabled(
    env: Mapping[str, str], modules: tuple[Module, ...] | None = None
) -> list[tuple[Module, Any]]:
    """Return ``(module, config)`` for every module that is configured in ``env``."""
    enabled: list[tuple[Module, Any]] = []
    names: set[str] = set()
    for module in modules or all_modules():
        if any(dep not in names for dep in module.requires):
            continue
        config = module.load(env) if module.load else True
        if config is None:
            continue
        enabled.append((module, config))
        names.add(module.name)
    return enabled
