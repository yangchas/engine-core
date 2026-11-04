"""Explicit error codes for data functions (C681, design/33).

A function that fails says WHAT failed and whether trying again can help; callers (the guardian handler, the driver)
read that instead of searching the error text. The codes live in ``guardian/error_codes.json``.

``CoreError`` keeps the old ``CODE:reason`` text form, so callers that still search the message keep working while the
producers are migrated.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

REGISTRY_PATH = Path(__file__).resolve().parent / "guardian" / "error_codes.json"


@lru_cache(maxsize=1)
def _document() -> dict[str, Any]:
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


def registry() -> dict[str, dict[str, Any]]:
    """code -> {retryable, exit, meaning[, reserved]}"""
    return dict(_document()["codes"])


def pending_codes() -> frozenset[str]:
    """Codes used as string literals before the registry existed; not classified yet."""
    return frozenset(_document().get("pending", {}).get("codes", []))


def lookup(code: str) -> dict[str, Any] | None:
    return _document()["codes"].get(code)


def is_retryable(code: str) -> bool | None:
    """True/False for a registered code, None for one the registry does not know."""
    spec = lookup(code)
    return None if spec is None else bool(spec["retryable"])


class CoreError(RuntimeError):
    """A failure with an explicit, registered code."""

    def __init__(self, code: str, reason: str = ""):
        spec = lookup(code)
        if spec is None or spec.get("reserved"):
            raise ValueError("unregistered_error_code:" + code)
        super().__init__(f"{code}:{reason}" if reason else code)
        self.error_code = code
        self.reason = reason
        self.retryable = bool(spec["retryable"])
        self.exit_code = int(spec["exit"])
