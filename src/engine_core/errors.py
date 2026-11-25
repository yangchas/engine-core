"""Explicit error codes for data functions (C681, design/33).

A function that fails says WHAT failed and whether trying again can help; callers (the guardian handler, the driver)
read that instead of searching the error text. The codes live in ``guardian/error_codes.json``.

``CoreError`` keeps the old ``CODE:reason`` text form, so callers that still search the message keep working while the
producers are migrated.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

REGISTRY_PATH = Path(__file__).resolve().parent / "guardian" / "error_codes.json"
FAILURE_DETAIL_MAX_CHARS = 2000

_SECRET = re.compile(
    r"(?i)(\b(?:TD_CONFIG|REDIS_URL|[\w-]*(?:token|password|cookie)[\w-]*)"
    r"[\"']?\s*(?:=|:)\s*)(?:\"[^\"\n]*\"|'[^'\n]*'|[^\s,;]+)"
)


def _redact(text: str) -> str:
    return _SECRET.sub(r"\1***", text)


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


class SchemaInvalid(CoreError, ValueError):
    """Data of a shape a function does not accept (still a ValueError for callers that catch that)."""

    def __init__(self, reason: str = ""):
        super().__init__("SCHEMA_INVALID", reason)


class QualityInvalid(CoreError, ValueError):
    """Data that fails a quality rule (still a ValueError for callers that catch that)."""

    def __init__(self, reason: str = ""):
        super().__init__("QUALITY_INVALID", reason)


class BudgetExhausted(CoreError, TimeoutError):
    """A run's time budget ran out (a TimeoutError, so existing handlers of timeouts still see it)."""

    def __init__(self, reason: str = ""):
        super().__init__("DAILY_KLINE_BUDGET_EXHAUSTED", reason)


# ---- the command boundary ------------------------------------------------------------------------------------------
class CommandFailed(RuntimeError):
    """A job script that exited non-zero, seen from the process that started it.

    ``error_code`` is what the script itself declared on its last stdout line (None when it declared nothing);
    ``unclassified`` is True when there is no declared code at all, so the caller can say so instead of guessing from the
    text. The message keeps the ``command_exit=N last_stderr_line=...`` form that callers and ledgers already show."""

    def __init__(self, message: str, *, returncode: int, error_code: str | None, retryable: bool | None,
                 unclassified: bool, reason: str = ""):
        super().__init__(message)
        self.returncode = returncode
        self.error_code = error_code
        self.declared_retryable = retryable
        self.unclassified = unclassified
        self.reason = reason


def declared_result(stdout: str) -> dict[str, Any] | None:
    """The job contract object on the last non-empty stdout line, if the script printed one."""
    lines = [line.strip() for line in str(stdout or "").splitlines() if line.strip()]
    if not lines:
        return None
    try:
        value = json.loads(lines[-1])
    except (TypeError, ValueError):
        return None
    # the job contract (contract: 1) or an older script that still just prints {"error_code": ...}
    return value if isinstance(value, dict) and (value.get("contract") == 1 or isinstance(value.get("error_code"), str)) else None


def command_error(returncode: int, stdout: str, stderr: str) -> CommandFailed:
    """What a failed job script means: its declared code when it printed one, otherwise 'unclassified'."""
    safe_stderr = _redact(str(stderr or "")).rstrip()
    safe_stdout = _redact(str(stdout or "")).rstrip()
    stderr_lines = [line.strip() for line in safe_stderr.splitlines() if line.strip()]
    root = ("last_stderr_line=" + stderr_lines[-1][:FAILURE_DETAIL_MAX_CHARS // 2]) if stderr_lines else ""
    tail = ("stderr_tail:\n" + safe_stderr) if safe_stderr else (("stdout_tail:\n" + safe_stdout) if safe_stdout else "")
    detail = root + ("\n" if root and tail else "")
    available = max(0, FAILURE_DETAIL_MAX_CHARS - len(detail))
    if len(tail) > available:
        omitted = len(tail) - available
        while True:
            marker = f"…[truncated {omitted} chars]"
            new_omitted = len(tail) - max(0, available - len(marker))
            if new_omitted <= omitted:
                break
            omitted = new_omitted
        tail = (marker + tail[omitted:]) if available > len(marker) else marker[:available]
    detail += tail
    message = f"command_exit={returncode}" + (" " + detail if detail else "")
    declared = declared_result(stdout) or {}
    code = declared.get("error_code") if isinstance(declared.get("error_code"), str) and declared.get("error_code") else None
    retryable = declared.get("retryable") if isinstance(declared.get("retryable"), bool) else None
    detail = declared.get("detail")
    reason = str(detail.get("error") if isinstance(detail, dict) and detail.get("error") else detail or "")[:200]
    return CommandFailed(message, returncode=returncode, error_code=code, retryable=retryable,
                         unclassified=code is None, reason=reason)
