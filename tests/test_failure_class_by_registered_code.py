# DATA: spec (failure class configuration of the shape the schedule will have once message patterns are gone)
"""C682 / design/33 step 2: a failure with a registered error code is classified by that code alone."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import guardian_handler  # noqa: E402

from engine_core.errors import CoreError  # noqa: E402

# the target shape: no text patterns, no code lists - only the default for what carries no code
BARE = {"failure_classes": {"default": "deterministic",
                            "deterministic": {"error_codes": [], "exception_types": [], "exit_codes": [],
                                              "message_patterns": []},
                            "retryable": {"error_codes": [], "exception_types": [], "exit_codes": [],
                                          "message_patterns": []}}}


@pytest.mark.parametrize("code,expected", [
    ("BAOSTOCK_GAP_UNRESOLVED", "retryable"), ("TD_RESOURCE_EXHAUSTED", "retryable"), ("RATE_LIMITED", "retryable"),
    ("UPSTREAM_NOT_READY", "retryable"), ("TD_UNAVAILABLE", "retryable"),
    ("TIER_NO_DATA", "deterministic"), ("CONFIG_ERROR", "deterministic"), ("POSTCONDITION_FAILED", "deterministic"),
])
def test_a_registered_code_decides_the_class_without_any_configured_list(code, expected):
    request = {"args": BARE}
    assert guardian_handler._classify_failure(request, error=CoreError(code, "x")) == expected


def test_a_failure_without_a_code_still_follows_the_configured_default():
    request = {"args": BARE}
    assert guardian_handler._classify_failure(request, error=RuntimeError("something odd")) == "deterministic"


def test_the_code_also_decides_when_it_arrives_inside_a_reply():
    request = {"args": BARE}
    reply = {"contract": 1, "status": "failed", "error_code": "TD_UNAVAILABLE", "retryable": True}
    assert guardian_handler._classify_failure(request, result=reply) == "retryable"
