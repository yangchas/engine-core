# DATA: real:td/errors/query_memory_exhausted_0x073a.json (real TD error text, wrapped the way TaosStore wraps it)
"""A TD query-memory exhaustion during the fill is not 'Baostock range failed' and is never final (C677)."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REAL = json.loads((ROOT / "tests" / "fixtures" / "real" / "td" / "errors"
                   / "query_memory_exhausted_0x073a.json").read_text(encoding="utf-8"))["message"]
WRAPPED = "td_statement_failed:ProgrammingError:" + REAL


def _run(monkeypatch, capsys, error):
    sys.path.insert(0, str(ROOT))
    from examples import backfill_td_daily_kline
    import engine_core.data_pipeline.daily_kline_fill as fill
    import engine_core.guardian.daily_kline_coverage as coverage

    def failing(*_a, **_k):
        raise error

    monkeypatch.setattr(fill, "run_daily_kline_fill_from_root", failing)
    monkeypatch.setattr(coverage, "td_settings", lambda database=None: {"history_min_trading_days": 10})
    monkeypatch.setattr(coverage, "make_store", lambda settings: type("S", (), {"close": lambda self: None})())
    monkeypatch.setenv("TD_CONFIG", "/nonexistent")
    code = backfill_td_daily_kline.main(["--date", "2026-10-21", "--artifact-root", str(ROOT)])
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_td_memory_exhaustion_is_a_retryable_td_failure_not_a_final_baostock_one(monkeypatch, capsys):
    code, reply = _run(monkeypatch, capsys, RuntimeError(WRAPPED))
    assert reply["retryable"] is True and code == 30
    assert reply["error_code"] == "TD_RESOURCE_EXHAUSTED"          # it is TD's pool, not a Baostock range problem


def test_a_genuine_range_error_is_still_final(monkeypatch, capsys):
    code, reply = _run(monkeypatch, capsys, ValueError("baostock returned a malformed row"))
    assert reply["error_code"] == "BAOSTOCK_RANGE_FAILED" and reply["retryable"] is False and code == 40


def test_a_baostock_timeout_stays_retryable_with_its_old_code(monkeypatch, capsys):
    code, reply = _run(monkeypatch, capsys, RuntimeError("BAOSTOCK_TIMEOUT: no range data"))
    assert reply["error_code"] == "BAOSTOCK_RANGE_FAILED" and reply["retryable"] is True and code == 30
