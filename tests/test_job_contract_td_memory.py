from taos.error import ProgrammingError

from engine_core.data_pipeline.job_contract import exception_code, failure_from_exception


def test_td_driver_memory_errors_are_classified():
    for errno, message in ((0x073A, "Query memory exhausted"), (0x0102, "Out of Memory")):
        exc = ProgrammingError(message, errno)
        assert exception_code(exc) == ("TD_RESOURCE_EXHAUSTED", True)
        result = failure_from_exception(exc, job="report")
        assert result.error_code == "TD_RESOURCE_EXHAUSTED"
        assert result.retryable is True
        assert "unclassified" not in result.detail

    other = ProgrammingError("Table does not exist", 0x0388)
    assert exception_code(other) == ("INTERNAL_ERROR", True)
    assert failure_from_exception(other, job="report").detail["unclassified"] is True

    unrelated = OSError(0x073A, "unrelated")
    assert exception_code(unrelated) == ("INTERNAL_ERROR", True)
