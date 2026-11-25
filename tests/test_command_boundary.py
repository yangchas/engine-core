# DATA: none for the parser tests; the end-to-end tests run the real job scripts in the sealed replay environment on an
# empty data root (no recorded rows involved)
"""C683 / design/33 step 3: the error code a job script declares reaches the guardian handler."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples"))
import guardian_handler  # noqa: E402

from engine_core.errors import CommandFailed, command_error, declared_result  # noqa: E402

BARE = {"failure_classes": {"default": "deterministic", "deterministic": {}, "retryable": {}}}


def _contract_line(code, retryable, **extra):
    return json.dumps({"contract": 1, "job": "x", "trade_date": "2026-10-23", "status": "failed", "tier": 0,
                       "source": None, "dataset": "", "version": None, "row_count": None, "coverage": None,
                       "error_code": code, "retryable": retryable, "attempts": 1, "elapsed_ms": 1.0, **extra})


def test_a_declared_code_is_read_from_the_last_stdout_line():
    stdout = "progress line\n" + _contract_line("UPSTREAM_NOT_READY", True, detail={"error": "daily_kline missing"})
    error = command_error(30, stdout, "warning: slow\n")
    assert isinstance(error, CommandFailed)
    assert (error.error_code, error.declared_retryable, error.unclassified) == ("UPSTREAM_NOT_READY", True, False)
    assert error.reason == "daily_kline missing"
    assert str(error).startswith("command_exit=30 last_stderr_line=warning: slow")     # the familiar text form stays


def test_a_script_that_declared_nothing_is_unclassified_not_guessed():
    error = command_error(1, "some report\n{not json", "Traceback ...\nValueError: boom")
    assert (error.error_code, error.unclassified) == (None, True)
    assert declared_result("") is None and declared_result("plain text") is None


def test_the_handler_classifies_by_the_declared_code_with_no_list_and_no_text_pattern():
    retryable = command_error(30, _contract_line("UPSTREAM_NOT_READY", True), "")
    assert guardian_handler._classify_failure({"args": BARE}, error=retryable) == "retryable"
    final = command_error(40, _contract_line("TIER_NO_DATA", False), "")
    assert guardian_handler._classify_failure({"args": BARE}, error=final) == "deterministic"
    code, reply = guardian_handler._failure_response({"args": BARE}, error=retryable)
    assert (code, reply["error_code"], reply["retryable"]) == (75, "UPSTREAM_NOT_READY", True)


# ---- the real job scripts, run in the sealed replay environment on an empty data root -----------------------------------
REPLAY_PYTHON = ROOT / "examples" / "replay_python"


def _run_command(tmp_path, script, *args):
    env = {**os.environ, "CORE_REPLAY_DIR": str(tmp_path / "replay"), "CORE_REPLAY_REAL_PYTHON": sys.executable,
           "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), str(ROOT)]), "CORE_DATA_ROOT": str(tmp_path / "root"),
           "CORE_PROVIDER_STATE": "memory", "HOME": str(tmp_path / "root")}
    (tmp_path / "root").mkdir(exist_ok=True)
    return subprocess.run([str(REPLAY_PYTHON), str(ROOT / "examples" / script), *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=120)


@pytest.mark.parametrize("script,args", [
    ("run_data_jobs.py", ["--job", "plate_members", "--date", "2026-10-23"]),
    ("fetch_registry_item.py", ["--item", "stock_day_facts", "--date", "2026-10-22"]),
    ("capture_kaipan_plates.py", ["--trade-date", "2026-10-23"]),
])
def test_a_missing_input_is_declared_as_upstream_not_ready_by_the_real_scripts(tmp_path, script, args):
    """Before this card these printed a usage error (exit 2), a bare exit 1 or a traceback: final failures by default."""
    result = _run_command(tmp_path, script, *args, "--artifact-root", str(tmp_path / "root"))
    assert result.returncode == 30, (result.returncode, result.stdout[-300:], result.stderr[-300:])
    declared = declared_result(result.stdout)
    assert declared is not None and declared["error_code"] == "UPSTREAM_NOT_READY" and declared["retryable"] is True
    failure = command_error(result.returncode, result.stdout, result.stderr)
    assert guardian_handler._classify_failure({"args": BARE}, error=failure) == "retryable"
    code, reply = guardian_handler._failure_response({"args": BARE}, error=failure)
    assert (code, reply["error_code"]) == (75, "UPSTREAM_NOT_READY")


def test_a_bad_argument_value_is_final_and_an_unexpected_failure_is_retryable_by_exception_type(tmp_path):
    bad = _run_command(tmp_path, "prep_trade_day.py", "--date", "2026-13-45", "--artifact-root", str(tmp_path / "root"))
    declared = declared_result(bad.stdout)
    assert bad.returncode == 40 and (declared["error_code"], declared["retryable"]) == ("DETERMINISTIC_FAILURE", False)
    assert declared["detail"]["unclassified"] is True                      # honest: nobody classified this one
    from engine_core.data_pipeline.job_contract import failure_from_exception

    outage = failure_from_exception(OSError("disk went away"), job="x", trade_date="2026-10-23")
    assert (outage.error_code, outage.retryable, outage.detail["unclassified"]) == ("INTERNAL_ERROR", True, True)


def test_the_core_data_daily_failure_is_a_contract_line_not_a_bare_exit_2(tmp_path):
    result = _run_command(tmp_path, "core_data_daily.py", "--phase", "postmarket", "--date", "2026-10-23")
    declared = declared_result(result.stdout)
    assert result.returncode in (30, 40) and declared is not None and declared["status"] == "failed"
    assert declared["error_code"] in ("DETERMINISTIC_FAILURE", "TD_UNAVAILABLE", "TD_VERIFY_FAILED")


def test_a_failure_from_the_boundary_text_form_is_unchanged_for_the_ledger(tmp_path):
    result = _run_command(tmp_path, "run_data_jobs.py", "--job", "plate_members", "--date", "2026-10-23",
                          "--artifact-root", str(tmp_path / "root"))
    failure = command_error(result.returncode, result.stdout, result.stderr)
    assert str(failure).startswith("command_exit=30") and "stdout_tail:" in str(failure)
