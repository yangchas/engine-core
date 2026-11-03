# DATA: real:td/errors/query_memory_exhausted_0x073a.json (real TD error text copied from the 2026-11-03 ledger)
"""TD running out of query memory is an infrastructure condition (another process holds the pool): retryable, never final."""
import json
from pathlib import Path

import pytest

from engine_core.guardian.contract import check_reply

ROOT = Path(__file__).resolve().parents[1]
REAL_ERROR = json.loads((ROOT / "tests" / "fixtures" / "real" / "td" / "errors"
                         / "query_memory_exhausted_0x073a.json").read_text(encoding="utf-8"))["message"]
# what TaosDdeStore/TaosStore wrap the client's text into (see data_pipeline/*_store._run): the exact ledger line of
# 2026-11-04 01:00:19 is `td_statement_failed:ProgrammingError:[0x073a]: Query memory exhausted`
WRAPPED = "td_statement_failed:ProgrammingError:" + REAL_ERROR


@pytest.fixture()
def handler():
    import sys
    sys.path.insert(0, str(ROOT))
    from examples import guardian_handler
    return guardian_handler


def test_a_wrapped_real_memory_exhaustion_error_is_retryable(handler):
    code, reply = handler._failure_response({"event_id": "daily_kline_backfill", "args": {}}, error=RuntimeError(WRAPPED))
    assert code == 75 and reply["retryable"] is True and reply["status"] == "failed"
    assert check_reply(code, reply) == []


def test_the_fill_entry_turns_it_into_a_retryable_reply_not_a_final_failure(handler, monkeypatch, tmp_path):
    import engine_core.data_pipeline.daily_kline_fill as fill

    def exhausted(root, target, args=None):
        raise RuntimeError(WRAPPED)

    monkeypatch.setattr(fill, "run_daily_kline_fill_from_root", exhausted)
    code, reply = handler._run_daily_kline_fill({"event_id": "daily_kline_backfill", "args": {}}, tmp_path,
                                                "2026-11-03", {})
    assert code == 75 and reply["retryable"] is True and check_reply(code, reply) == []


def test_a_genuine_programming_error_is_still_final(handler):
    code, reply = handler._failure_response({"event_id": "daily_kline_backfill", "args": {}},
                                            error=ValueError("target_not_in_trade_calendar:2026-11-03"))
    assert code == 40 and reply["error_code"] == "DETERMINISTIC_FAILURE"
