"""Errors shared by all modules, and normalisation of tool arguments."""

from __future__ import annotations


class ConfigError(ValueError):
    """Configuration is missing or inconsistent. The server refuses to start."""


class SourceError(Exception):
    """One upstream system failed. A tool returns it as a dict instead of raising."""

    def __init__(self, source: str, message: str):
        super().__init__(f"[{source}] {message}")
        self.source = source
        self.message = message

    def to_dict(self) -> dict[str, str]:
        return {"error": self.message, "source": self.source}


def clamp_days(days: int | None, default: int, max_days: int = 31) -> int:
    try:
        value = int(days) if days is not None else default
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, max_days))
