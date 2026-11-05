"""design/33 S5: a failed prep step reports the registered code its script declared, not a text blob."""
import io
from contextlib import redirect_stdout

import pytest

from engine_core.data_pipeline.job_contract import failure_from_exception, emit
from engine_core.data_pipeline.td_sink import TdUnavailable
from engine_core.errors import CoreError
from engine_core.prep_trade_day import _declared_code


def _script_output(exc: Exception, *, stderr: str = "") -> str:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        emit(failure_from_exception(exc, job="build_plate_amount_baseline", trade_date="2026-10-23"))
    return buffer.getvalue() + stderr


def test_the_code_is_read_from_the_contract_line_even_when_stderr_follows_it():
    output = _script_output(TdUnavailable("td_connect_failed:x"), stderr="Traceback (most recent call last):\n  boom\n")
    assert _declared_code(output) == "TD_UNAVAILABLE"


@pytest.mark.parametrize("output", ["", "Traceback (most recent call last):\nValueError: x\n",
                                    '{"contract":1,"error_code":"SOME_CODE_NOBODY_REGISTERED"}'])
def test_no_declared_or_an_unregistered_code_is_none(output):
    assert _declared_code(output) is None


def test_prep_failure_prefers_a_retryable_declared_code_and_stays_plain_without_one():
    from examples.core_guardian import _prep_failure
    err = _prep_failure({"error_codes": ["CONTRACT_INVALID", "TD_UNAVAILABLE"]}, ["a:COMMAND_FAILED"])
    assert isinstance(err, CoreError) and err.error_code == "TD_UNAVAILABLE"
    plain = _prep_failure({"error_codes": []}, ["a:COMMAND_FAILED"])
    assert not isinstance(plain, CoreError) and str(plain).startswith("prep_errors:")


def test_a_missing_symbol_universe_artifact_is_upstream_not_ready_not_a_bare_runtime_error():
    """The entry scripts used to raise RuntimeError('symbol_universe_artifact_required:...'): unclassified in the replay."""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for script in ("run_data_jobs.py", "static_data_refresh.py"):
        text = (root / "examples" / script).read_text(encoding="utf-8")
        assert not re.search(r'RuntimeError\(\s*"symbol_universe_artifact_required', text), script
        assert 'CoreError("UPSTREAM_NOT_READY", "symbol_universe_artifact_required' in text, script
