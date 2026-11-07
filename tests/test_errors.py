import pytest

from engine_core.errors import is_retryable, lookup
from engine_core.data_pipeline.baostock_completion import evaluate_baostock_completion


def test_backfill_error_codes_and_completion_failure():
    for code, retryable in (("BAOSTOCK_RANGE_FAILED", True),
                            ("TIER_ATTEMPTS_EXHAUSTED", False),
                            ("known_missing_incomplete", False)):
        assert lookup(code) is not None
        assert is_retryable(code) is retryable
    with pytest.raises(ValueError) as raised:
        evaluate_baostock_completion(["000001"], [], {}, coverage_threshold=1.0, max_source_empty=0)
    assert raised.value.error_code == "known_missing_incomplete"
    assert raised.value.retryable is False
