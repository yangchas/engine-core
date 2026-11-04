# DATA: real:td/errors/query_memory_exhausted_0x073a.json, td/errors/table_does_not_exist.json (errno and text of real TD errors)
"""C682 / design/33 step 2: the producers raise explicit, registered codes, and each is read as retryable by code alone."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples"))
import guardian_handler  # noqa: E402

from engine_core.errors import CoreError  # noqa: E402

ERRORS = ROOT / "tests" / "fixtures" / "real" / "td" / "errors"
BARE = {"failure_classes": {"default": "deterministic", "deterministic": {}, "retryable": {}}}   # no lists, no patterns


def _real_driver_error(name):
    record = json.loads((ERRORS / f"{name}.json").read_text(encoding="utf-8"))

    class ProgrammingError(Exception):          # the shape of the TD client's exception: message text and errno
        pass

    error = ProgrammingError(record["message"])
    error.errno = int(record["errno"], 16)
    return error


def _classified(error):
    return guardian_handler._classify_failure({"args": BARE}, error=error)


def test_td_boundary_turns_the_real_memory_exhaustion_error_into_its_own_code():
    from engine_core.data_pipeline.td_sink import td_statement_error

    exhausted = td_statement_error(_real_driver_error("query_memory_exhausted_0x073a"), "td_statement_failed:x")
    assert (exhausted.error_code, exhausted.retryable) == ("TD_RESOURCE_EXHAUSTED", True)
    other = td_statement_error(_real_driver_error("table_does_not_exist"), "td_statement_failed:y")
    assert (other.error_code, other.retryable) == ("TD_UNAVAILABLE", True)
    assert _classified(exhausted) == "retryable" and _classified(other) == "retryable"


def test_the_exhaustion_is_recognised_from_the_errno_even_when_the_text_changes():
    from engine_core.data_pipeline.td_sink import td_statement_error

    error = _real_driver_error("query_memory_exhausted_0x073a")
    error.args = ("a different wording",)
    assert td_statement_error(error, "x").error_code == "TD_RESOURCE_EXHAUSTED"


def test_the_fill_aborts_with_the_gap_code():
    from engine_core.data_pipeline.daily_kline_fill import _abort

    error = _abort("consecutive_failures", {"attempted": 5})
    assert (error.error_code, error.retryable) == ("BAOSTOCK_GAP_UNRESOLVED", True)
    assert str(error).startswith("BAOSTOCK_GAP_UNRESOLVED:consecutive_failures:")
    assert _classified(error) == "retryable"


def test_the_request_budget_refuses_with_the_rate_limit_code():
    from engine_core.data_pipeline.jobs import RequestBudget

    budget = RequestBudget(max_requests=1, min_interval_seconds=10)
    budget.consume("kaipan", at=0)
    with pytest.raises(CoreError) as refused:
        budget.consume("kaipan", at=1)
    assert refused.value.error_code == "RATE_LIMITED" and _classified(refused.value) == "retryable"


def test_upstream_not_ready_keeps_its_reason_code_and_is_retryable_by_its_error_code():
    from engine_core.data_pipeline.derived_daily import UpstreamNotReady

    error = UpstreamNotReady("DAILY_KLINE_MISSING", "2026-10-23")
    assert (error.error_code, error.code, error.detail) == ("UPSTREAM_NOT_READY", "DAILY_KLINE_MISSING", "2026-10-23")
    assert str(error) == "UPSTREAM_NOT_READY:DAILY_KLINE_MISSING:2026-10-23" and _classified(error) == "retryable"


def test_a_startup_probe_that_times_out_raises_its_code():
    import time
    from engine_core.guardian.startup import _timed_probe

    with pytest.raises(CoreError) as timed_out:
        _timed_probe(lambda: time.sleep(2), timeout=0.05)
    assert timed_out.value.error_code == "PROBE_TIMEOUT" and _classified(timed_out.value) == "retryable"


def test_td_refuses_a_non_baostock_partition_with_its_code_and_stays_a_value_error():
    from engine_core.data_pipeline.td_sink import TdSourceRejected

    error = TdSourceRejected("wencai")
    assert isinstance(error, ValueError) and isinstance(error, CoreError)
    assert (error.error_code, error.reason) == ("TD_REQUIRES_BAOSTOCK", "wencai")
    assert _classified(error) == "retryable"


def test_baostock_connection_and_login_failures_carry_their_codes():
    from engine_core.data_pipeline.providers.baostock_daily_provider import BaostockDailyProvider  # noqa: F401
    import engine_core.daily_kline_capture as capture
    import inspect

    # the sources raise CoreError (not a bare RuntimeError): checked on the source so no Baostock session is needed
    source = inspect.getsource(capture)
    assert 'CoreError("BAOSTOCK_CONNECTION_ERROR")' in source and 'CoreError("BAOSTOCK_LOGIN_FAILED"' in source
    assert 'RuntimeError("BAOSTOCK_CONNECTION_ERROR")' not in source
