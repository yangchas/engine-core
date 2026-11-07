import pytest
import subprocess
import sys

from engine_core.errors import is_retryable, lookup
from engine_core.data_pipeline.baostock_completion import evaluate_baostock_completion


def test_command_error_preserves_real_traceback_tail():
    from engine_core import errors
    script = "def fail():\n    raise ValueError('root cause ' + 'x' * 900)\nfail()"
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)
    message = str(errors.command_error(result.returncode, result.stdout, result.stderr))
    assert message.startswith("command_exit=1 last_stderr_line=ValueError: root cause")
    assert "Traceback (most recent call last):" in message
    assert len(message) <= getattr(errors, "FAILURE_DETAIL_MAX_CHARS", 2000) + len("command_exit=1 ")
    long_message = str(errors.command_error(result.returncode, result.stdout, result.stderr * 4))
    assert "…[truncated " in long_message
    assert long_message.endswith(result.stderr.strip())
    assert len(long_message) <= errors.FAILURE_DETAIL_MAX_CHARS + len("command_exit=1 ")
    from examples import guardian_handler
    assert guardian_handler._failure_response({}, error=errors.command_error(
        result.returncode, result.stdout, result.stderr))[1]["detail"] == message


def test_command_error_redacts_credentials():
    from engine_core.errors import command_error
    message = str(command_error(1, "", "REDIS_URL=redis://u:p@host token=abc123"))
    assert "p@host" not in message
    assert "abc123" not in message


def test_backfill_error_codes_and_completion_failure():
    for code, retryable in (("BAOSTOCK_RANGE_FAILED", True),
                            ("TIER_ATTEMPTS_EXHAUSTED", False),
                            ("KNOWN_MISSING_INCOMPLETE", False)):
        assert lookup(code) is not None
        assert is_retryable(code) is retryable
    with pytest.raises(ValueError) as raised:
        evaluate_baostock_completion(["000001"], [], {}, coverage_threshold=1.0, max_source_empty=0)
    assert raised.value.error_code == "KNOWN_MISSING_INCOMPLETE"
    assert raised.value.retryable is False
