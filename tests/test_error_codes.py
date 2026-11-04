# DATA: spec (the registry is declared configuration); the static scan reads the repository source
"""C681 / design/33 step 1: the error code registry, CoreError, and a guard against unregistered codes."""
import json
import re
import subprocess
from datetime import date
from pathlib import Path

import pytest

from engine_core import errors
from engine_core.guardian.contract import check_reply

ROOT = Path(__file__).resolve().parents[1]
CODE_LITERAL = re.compile(r"[\"']([A-Z][A-Z0-9_]{2,})[\"']\s*$")
FINAL_EXITS = {40, 64}


def test_every_registered_code_is_well_formed_and_coherent_with_the_reply_contract():
    for code, spec in errors.registry().items():
        assert re.fullmatch(r"[A-Z][A-Z0-9_]+", code), code
        assert isinstance(spec["retryable"], bool) and spec["meaning"], code
        if spec.get("reserved"):
            continue
        status = "not_applicable" if spec["exit"] == 20 else "failed"
        reply = {"contract": 1, "status": status, "error_code": code, "retryable": spec["retryable"]}
        assert check_reply(spec["exit"], reply) == [], (code, spec)         # the exit code the registry names is legal
        if spec["retryable"]:
            assert spec["exit"] in (30, 75), code
        elif spec["exit"] != 20:
            assert spec["exit"] in FINAL_EXITS, code


def test_pending_codes_are_unregistered_and_not_past_their_expiry():
    document = json.loads(errors.REGISTRY_PATH.read_text(encoding="utf-8"))
    assert not (errors.pending_codes() & set(errors.registry()))
    assert document["pending"]["expires"] >= date.today().isoformat(), "classify the pending codes (S2) or renew"


def test_core_error_carries_the_registry_facts_and_keeps_the_text_form():
    error = errors.CoreError("BAOSTOCK_GAP_UNRESOLVED", "coverage_or_evidence_incomplete:2026-10-23")
    assert isinstance(error, RuntimeError)
    assert (error.error_code, error.retryable, error.exit_code) == ("BAOSTOCK_GAP_UNRESOLVED", True, 30)
    assert str(error) == "BAOSTOCK_GAP_UNRESOLVED:coverage_or_evidence_incomplete:2026-10-23"
    final = errors.CoreError("TIER_NO_DATA")
    assert (final.retryable, final.exit_code, str(final)) == (False, 40, "TIER_NO_DATA")


@pytest.mark.parametrize("code", ["NOT_A_REGISTERED_CODE", "INTERNAL_ERROR"])
def test_core_error_refuses_unknown_and_reserved_codes(code):
    with pytest.raises(ValueError, match="unregistered_error_code"):
        errors.CoreError(code, "x")


def test_handler_defaults_carry_no_code_lists_and_no_text_patterns():
    """The registry is the single place that says which code is retryable; the handler's defaults only name exit
    statuses and exception types (design/33 step 4)."""
    import sys
    sys.path.insert(0, str(ROOT / "examples"))
    import guardian_handler

    defaults = guardian_handler._failure_classes({})
    for category in ("retryable", "deterministic"):
        assert not defaults[category].get("error_codes") and not defaults[category].get("message_patterns"), category
    assert "retryable_first" not in defaults and defaults["default"] == "retryable"


def _literal_codes() -> dict[str, str]:
    out = subprocess.run(
        ["grep", "-rnoE", r"error_code[\"']?\s*[:=]\s*[\"'][A-Z][A-Z0-9_]{2,}[\"']"
                          r"|\"error_code\":\s*\"[A-Z][A-Z0-9_]{2,}\""
                          r"|\"code\":\s*\"[A-Z][A-Z0-9_]{2,}\"", "src", "examples", "--include=*.py"],
        cwd=ROOT, capture_output=True, text=True).stdout
    found: dict[str, str] = {}
    for line in out.splitlines():
        match = CODE_LITERAL.search(line)
        if match and not match.group(1).endswith("_"):
            found.setdefault(match.group(1), line.split(":")[0] + ":" + line.split(":")[1])
    return found


def test_no_error_code_literal_is_unknown_to_the_registry():
    """A new `error_code` string must be registered (or consciously listed as pending), not invented in place."""
    known = set(errors.registry()) | errors.pending_codes()
    unknown = {code: where for code, where in _literal_codes().items() if code not in known}
    assert not unknown, "register these in src/engine_core/guardian/error_codes.json:\n" + "\n".join(
        f"  {code}  (first seen {where})" for code, where in sorted(unknown.items()))


def test_pending_list_has_no_stale_entries():
    used = set(_literal_codes())
    stale = sorted(errors.pending_codes() - used)
    assert not stale, f"no longer used as a literal anywhere, remove from pending: {stale}"
