"""The small shared layer every module's tools go through."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from firma_mcp.errors import SourceError

log = logging.getLogger("firma_mcp")

# Every tool in this server only reads. `openWorldHint` is false because each tool talks to
# one bounded private account, not the open internet.
READ_ONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}


def listing(items: list[dict]) -> dict:
    return {"items": items, "count": len(items)}


def run_tool(name: str, guard: Callable[[], object], fn: Callable[[], dict]) -> dict:
    """Common wrapper for a tool call: access check, timing, errors as data.

    ``guard()`` runs outside the try block so that a denied call raises to the client
    unchanged. Anything the upstream call raises becomes ``{"error", "source"}`` instead:
    one failing system must not break a client that is reading several. Tool arguments are
    never logged, since they can carry mail subjects, amounts and invoice numbers.
    """
    caller = guard()
    started = time.monotonic()
    try:
        result = fn()
        log.info("tool=%s caller=%s ok %.2fs", name, caller, time.monotonic() - started)
        return result
    except SourceError as exc:
        log.warning(
            "tool=%s caller=%s source=%s error %.2fs", name, caller, exc.source, time.monotonic() - started
        )
        return exc.to_dict()
    except Exception as exc:  # noqa: BLE001 - tool boundary: never a bare exception to the client
        log.exception("tool=%s caller=%s unexpected error %.2fs", name, caller, time.monotonic() - started)
        return {"error": f"{name}: {exc}", "source": name.split("_")[0]}


@dataclass
class Context:
    """What the server hands to each module while it registers its tools."""

    guard: Callable[[], object]
    # Clients published by modules registered earlier, keyed by module name. This is how a
    # module uses another one when both are enabled (KSeF marks invoices booked in Merit).
    services: dict[str, Any] = field(default_factory=dict)
    # Test doubles keyed by module name; when present a module uses it instead of a real client.
    overrides: dict[str, Any] = field(default_factory=dict)
    # Whether modules may start background threads (off in tests).
    background: bool = False

    def run(self, name: str, fn: Callable[[], dict]) -> dict:
        return run_tool(name, self.guard, fn)
