"""Reply contract of a guardian data function: (exit code, JSON reply) pairs a function may produce.

guardiand maps a pair to an outcome in ``decodeResult`` (native/guardiand/runtime.go); the pairs it understands are in
``contract_matrix.json``. A pair guardiand does not understand is not rejected by it, it silently becomes a generic
retryable ``INTERNAL_ERROR`` (an exit-20 no-op without ``ALREADY_SATISFIED`` was retried to FAILED_FINAL that way).
"""
from __future__ import annotations

from typing import Any, Mapping

STATUSES = frozenset({"ok", "degraded", "not_applicable", "failed"})


def check_reply(exit_code: int, reply: Any) -> list[str]:
    """Violations of the reply contract; an empty list means guardiand will read the pair as the function meant it."""
    if not isinstance(reply, Mapping):
        return ["reply_missing_or_not_json_object"]
    problems: list[str] = []
    if reply.get("contract") != 1:
        problems.append("contract_field_must_be_1")
    status, code, retryable = reply.get("status"), reply.get("error_code"), reply.get("retryable")
    if status not in STATUSES:
        problems.append(f"status_not_in_{sorted(STATUSES)}")
    if not isinstance(code, str):
        problems.append("error_code_must_be_a_string")
        code = ""
    if not isinstance(retryable, bool):
        problems.append("retryable_must_be_a_bool")
    if problems:
        return problems
    if exit_code == 0:
        if status == "failed":
            problems.append("exit_0_cannot_report_failed")
    elif exit_code == 10:
        if status != "degraded" or retryable:
            problems.append("exit_10_is_degraded_and_not_retryable")
    elif exit_code == 20:
        if status != "not_applicable" or code != "ALREADY_SATISFIED":
            problems.append("exit_20_requires_not_applicable_and_ALREADY_SATISFIED")
    elif exit_code == 30:
        if status != "failed" or not retryable or not code:
            problems.append("exit_30_is_failed_retryable_with_error_code")
    elif exit_code == 40:
        if status != "failed" or retryable or not code:
            problems.append("exit_40_is_failed_not_retryable_with_error_code")
    elif exit_code == 64:
        if status != "failed" or retryable or code != "CONFIG_ERROR":
            problems.append("exit_64_is_CONFIG_ERROR_not_retryable")
    elif exit_code == 75:
        if status != "failed" or not retryable or not code:
            problems.append("exit_75_is_failed_retryable_with_error_code")
    else:
        problems.append(f"exit_{exit_code}_is_not_a_contract_exit_code")
    return problems
