# DATA: spec (failure class configuration shapes; no rows involved)
"""C684 / design/33 step 4: failures are classified by registered code, exit status and exception type - never by text."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples"))
import guardian_handler  # noqa: E402

from engine_core.errors import CommandFailed, CoreError  # noqa: E402

# the configuration as it still is on the live guardian: text patterns present, default deterministic
LEGACY_CONFIG = {"failure_classes": {
    "default": "deterministic",
    "deterministic": {"exit_codes": [2, 40, 64], "exception_types": ["ValueError", "KeyError"], "error_codes": [],
                      "message_patterns": []},
    "retryable": {"exit_codes": [30, 75], "exception_types": ["TimeoutError", "ConnectionError"], "error_codes": [],
                  "message_patterns": ["rate limit", "connection error", "receive exception"]},
    "retryable_first": {"message_patterns": ["baostock_gap_unresolved", "query memory exhausted", "0x073a"]}}}
FORMER_TEXT = ["rate limit exceeded", "Connection error", "query memory exhausted", "[0x073a]", "BAOSTOCK_GAP_UNRESOLVED:x",
               "receive exception", "taos.error.ProgrammingError", "daily_kline_missing", "upstream_not_ready"]


@pytest.mark.parametrize("text", FORMER_TEXT)
def test_text_in_an_error_message_decides_nothing_even_when_the_old_patterns_are_configured(text):
    category = guardian_handler._classify_failure({"args": LEGACY_CONFIG}, error=RuntimeError(text))
    assert category == "deterministic"          # no code, no exit status, no known type: the configured default


def test_without_any_configuration_an_unclassified_failure_is_retryable_and_says_so():
    code, reply = guardian_handler._failure_response({"args": {}}, error=RuntimeError("something nobody classified"))
    assert code == 75 and reply["retryable"] is True
    assert reply["error_code"] == "INTERNAL_ERROR" and reply["unclassified"] is True


def test_programming_errors_are_final_by_their_type_not_by_default():
    code, reply = guardian_handler._failure_response({"args": {}}, error=ValueError("bad value"))
    assert code == 40 and reply["retryable"] is False and "unclassified" not in reply


def test_a_final_failure_keeps_its_real_code_instead_of_the_generic_one():
    code, reply = guardian_handler._failure_response({"args": {}}, error=CoreError("TIER_NO_DATA", "no rows"))
    assert (code, reply["error_code"], reply["retryable"]) == (40, "TIER_NO_DATA", False)
    assert "unclassified" not in reply


@pytest.mark.parametrize("returncode,expected", [(30, "retryable"), (75, "retryable"), (40, "deterministic"),
                                                 (2, "deterministic")])
def test_the_exit_status_of_a_script_that_declared_nothing_still_counts(returncode, expected):
    failure = CommandFailed("command_exit=%d" % returncode, returncode=returncode, error_code=None, retryable=None,
                            unclassified=True)
    assert guardian_handler._classify_failure({"args": LEGACY_CONFIG}, error=failure) == expected


def test_a_script_that_declared_nothing_and_exited_with_an_unknown_status_is_unclassified_and_retryable():
    failure = CommandFailed("command_exit=1", returncode=1, error_code=None, retryable=None, unclassified=True)
    code, reply = guardian_handler._failure_response({"args": {}}, error=failure)
    assert code == 75 and reply["unclassified"] is True and reply["error_code"] == "INTERNAL_ERROR"


def test_a_fetch_job_failure_carries_its_exit_status_as_a_fact_not_a_string():
    from engine_core.guardian.startup import FetchJobError

    failure = FetchJobError("fetch_job_failed:30", "FETCH_EXIT_30", returncode=30)
    assert failure.returncode == 30
    assert guardian_handler._classify_failure({"args": LEGACY_CONFIG}, error=failure) == "retryable"


def test_the_schedule_configuration_no_longer_carries_text_patterns():
    timeline = json.loads((ROOT / "src/engine_core/guardian/timeline.json").read_text(encoding="utf-8"))
    for name, function in timeline["functions"].items():
        classes = (function.get("args") or {}).get("failure_classes")
        if not classes:
            continue
        assert "retryable_first" not in classes, name
        for category in ("deterministic", "retryable"):
            assert not classes.get(category, {}).get("message_patterns"), (name, category)
