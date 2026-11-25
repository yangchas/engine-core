# DATA: real:td/errors/query_memory_exhausted_0x073a.json, td/errors/table_does_not_exist.json (errno and text of real TD errors)
"""C685 / design/33 step 4b: the remaining places that guessed from message text now read codes and types."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ERRORS = ROOT / "tests" / "fixtures" / "real" / "td" / "errors"

from engine_core.data_pipeline.job_contract import exception_code  # noqa: E402
from engine_core.errors import BudgetExhausted, CoreError, QualityInvalid, SchemaInvalid  # noqa: E402


def _driver_error(name, *, with_errno=True, message=None):
    record = json.loads((ERRORS / f"{name}.json").read_text(encoding="utf-8"))

    class ProgrammingError(Exception):
        pass

    error = ProgrammingError(message if message is not None else record["message"])
    if with_errno:
        error.errno = int(record["errno"], 16)
    return error


def test_exception_code_reads_codes_and_types_not_messages():
    assert exception_code(SchemaInvalid("required_field_missing")) == ("SCHEMA_INVALID", False)
    assert exception_code(QualityInvalid("empty_response")) == ("QUALITY_INVALID", False)
    assert exception_code(BudgetExhausted()) == ("DAILY_KLINE_BUDGET_EXHAUSTED", True)
    assert exception_code(TimeoutError("x"), "baostock_query_all_stock") == ("BAOSTOCK_TIMEOUT", True)
    assert exception_code(TimeoutError("x"), "wencai") == ("FETCH_TIMEOUT", True)
    assert exception_code(KeyError("k")) == ("DETERMINISTIC_FAILURE", False)
    assert exception_code(OSError("disk")) == ("INTERNAL_ERROR", True)
    # a message that merely MENTIONS a condition decides nothing
    assert exception_code(RuntimeError("quality_invalid:looks_like_a_prefix")) == ("INTERNAL_ERROR", True)
    assert exception_code(RuntimeError("operation timeout while ...")) == ("INTERNAL_ERROR", True)


def test_the_new_exception_types_stay_compatible_with_the_old_ones():
    assert isinstance(SchemaInvalid("x"), ValueError) and isinstance(QualityInvalid("x"), ValueError)
    assert isinstance(BudgetExhausted(), TimeoutError) and isinstance(BudgetExhausted(), CoreError)
    assert str(SchemaInvalid("symbol_missing")) == "SCHEMA_INVALID:symbol_missing"


def test_td_errors_are_read_by_number_first():
    from engine_core.data_pipeline.td_sink import td_memory_exhausted, td_object_missing

    exhausted = _driver_error("query_memory_exhausted_0x073a")
    missing = _driver_error("table_does_not_exist")
    assert td_memory_exhausted(exhausted) and not td_memory_exhausted(missing)
    assert td_object_missing(missing) and not td_object_missing(exhausted)
    reworded = _driver_error("query_memory_exhausted_0x073a", message="a different wording")
    assert td_memory_exhausted(reworded)                                   # the number is enough
    # a word that only looks related does not count any more ("memory" used to be enough)
    assert not td_memory_exhausted(RuntimeError("segment memory map is corrupt"))
    assert not td_object_missing(RuntimeError("something else entirely"))


def test_plate_amount_query_splits_its_range_only_for_the_real_memory_error():
    from engine_core.data_pipeline import auction_amount_facts as baseline

    class Cursor:
        def __init__(self, error):
            self.error, self.calls = error, []

        def execute(self, sql):
            self.calls.append(sql)
            if len(self.calls) == 1 and self.error is not None:
                raise self.error

        def fetchall(self):
            return []

    days = ["2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23"]
    split = Cursor(_driver_error("query_memory_exhausted_0x073a"))
    baseline._read_0945_slice(split, days, set(days), None, td_split_retry_depth=1)
    assert len(split.calls) == 3                                           # failed once, then two halves
    other = Cursor(_driver_error("table_does_not_exist"))
    with pytest.raises(Exception):
        baseline._read_0945_slice(other, days, set(days), None, td_split_retry_depth=1)
    assert len(other.calls) == 1                                           # not a memory problem: no splitting
    default = Cursor(_driver_error("query_memory_exhausted_0x073a"))
    with pytest.raises(Exception):
        baseline._read_0945_slice(default, days, set(days), None)
    assert len(default.calls) == 1


def test_the_daily_kline_chain_tells_budget_exhaustion_by_type():
    import inspect
    from engine_core.data_pipeline.providers import daily_kline_chain

    source = inspect.getsource(daily_kline_chain)
    assert "isinstance(exc, BudgetExhausted)" in source
    assert '"DAILY_KLINE_BUDGET_EXHAUSTED" in str(exc)' not in source


def test_a_plan_that_could_not_be_built_is_reported_as_such_not_as_an_unknown_item(tmp_path):
    """Found by the whole-day replay: the PLAN_ERROR row was applied as if '*' were an item (UNKNOWN_ITEM)."""
    from engine_core.guardian.startup import boot_apply_plan

    result = boot_apply_plan("2026-10-23", [{"id": "*", "action": "PLAN_ERROR", "error": "TdUnavailable"}], tmp_path,
                             now="2026-10-23T08:50:00+08:00", phase="PRE_MARKET", dry_run=True)
    assert result["status"] == "failed" and result["error_code"] == "PLAN_UNAVAILABLE" and result["retryable"] is True
    row = result["detail"]["results"][0]
    assert (row["action"], row["error_code"], row["error"]) == ("PLAN_ERROR", "PLAN_UNAVAILABLE", "TdUnavailable")
    import sys
    sys.path.insert(0, str(ROOT / "examples"))
    import guardian_handler
    status, reply = guardian_handler._failure_response({"args": {}}, result=result)
    assert (status, reply["error_code"], reply["retryable"]) == (75, "PLAN_UNAVAILABLE", True)
    assert "unclassified" not in reply
