from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import sys
from zoneinfo import ZoneInfo

import pytest

import examples.run_task008_replay_opening_validation as replay_tool
from examples.run_task008_replay_opening_validation import (
    _load_rows,
    _projection,
    _run_pass,
    main,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _row(symbol: str, *, price: str, source_time_ms: int) -> dict[str, str]:
    return {
        "symbol": symbol,
        "px": price,
        "pc": "10000",
        "amt": "1000000",
        "amt2m": "120000",
        "ls": "0",
        "ts": str(source_time_ms),
    }


def test_replay_projection_keeps_symbol_payload_when_input_is_shuffled():
    observed_at = datetime(2026, 9, 15, 9, 32, 10, tzinfo=SHANGHAI)
    source_time_ms = int((observed_at - timedelta(seconds=1)).timestamp() * 1000)
    rows = [
        _row("000001", price="11850", source_time_ms=source_time_ms),
        _row("000002", price="3050", source_time_ms=source_time_ms),
    ]

    ordered = _run_pass(
        rows,
        trade_date="2026-09-15",
        observed_at=observed_at,
        stale_after_ms=60_000,
        symbols=("000001", "000002"),
        shuffled=False,
    )
    reversed_rows = list(reversed(rows))
    shuffled = _run_pass(
        reversed_rows,
        trade_date="2026-09-15",
        observed_at=observed_at,
        stale_after_ms=60_000,
        symbols=("000001", "000002"),
        shuffled=False,
    )

    assert ordered["projection"]["content_hash"] == shuffled["projection"]["content_hash"]
    assert ordered["engine"]["000001"]["content_hash"] == shuffled["engine"]["000001"]["content_hash"]
    assert ordered["engine"]["000002"]["content_hash"] == shuffled["engine"]["000002"]["content_hash"]
    assert ordered["engine"]["000001"]["opening_fact"]["change_pct"] > 0
    assert ordered["engine"]["000002"]["opening_fact"]["change_pct"] < 0


def test_replay_of_previous_trade_date_stays_partial_and_never_ready():
    observed_at = datetime(2026, 9, 15, 9, 32, 10, tzinfo=SHANGHAI)
    projection = _projection(
        [_row("000001", price="11850", source_time_ms=1789369200000)],
        trade_date="2026-09-15",
        observed_at=observed_at,
        stale_after_ms=60_000,
    )

    assert projection.status.value == "PARTIAL"
    assert projection.coverage == 1.0
    assert projection.stale_symbols == ("000001",)
    assert "trade_date" in projection.quotes["000001"].field_errors
    assert "stale" in projection.quotes["000001"].field_errors


def test_replay_cutoff_exposes_future_source_times_as_an_explicit_error():
    observed_at = datetime(2026, 9, 18, 9, 32, 10, tzinfo=SHANGHAI)
    result = _run_pass(
        [_row("000001", price="11850", source_time_ms=1789716570000)],
        trade_date="2026-09-18",
        observed_at=observed_at,
        stale_after_ms=60_000,
        symbols=("000001",),
        shuffled=False,
    )

    assert result["projection"]["status"] == "PARTIAL"
    assert result["projection"]["field_error_counts"] == {"future_ts": 1}
    assert result["engine_input"]["sample_quote_count"] == 0
    assert result["engine"]["000001"]["fact_status"] == "MISSING"
    assert result["engine"]["000001"]["opening_fact"] is None


def test_future_q2_values_are_excluded_before_engine_but_safe_rows_remain(monkeypatch):
    observed_at = datetime(2026, 9, 15, 9, 32, 10, tzinfo=SHANGHAI)
    observed_ms = int(observed_at.timestamp() * 1000)
    previous_day_ms = int(
        datetime(2026, 9, 14, 15, 0, tzinfo=SHANGHAI).timestamp() * 1000
    )
    missing_time_row = _row("000004", price="4010", source_time_ms=observed_ms)
    del missing_time_row["ts"]
    rows = [
        _row("000001", price="11850", source_time_ms=observed_ms + 1_000),
        _row("000002", price="3050", source_time_ms=observed_ms - 1_000),
        _row("000003", price="2200", source_time_ms=previous_day_ms),
        missing_time_row,
    ]
    engine_inputs = []

    def inspect_engine_input(projection, **kwargs):
        engine_inputs.append((kwargs["symbol"], projection))
        return {
            "symbol": kwargs["symbol"],
            "content_hash": projection.content_hash,
        }

    monkeypatch.setattr(replay_tool, "_engine_result", inspect_engine_input)
    result = _run_pass(
        rows,
        trade_date="2026-09-15",
        observed_at=observed_at,
        stale_after_ms=60_000,
        symbols=("000001", "000002", "000003", "000004"),
        shuffled=False,
    )

    assert result["projection"]["field_error_counts"] == {
        "future_ts": 1,
        "stale": 1,
        "trade_date": 1,
        "ts": 1,
    }
    assert result["engine_input"]["eligible_quote_count"] == 1
    assert result["engine_input"]["excluded_future_count"] == 1
    assert result["engine_input"]["excluded_trade_date_mismatch_count"] == 1
    assert result["engine_input"]["excluded_missing_source_time_count"] == 1
    assert result["engine_input"]["historical_available_at_status"] == "UNKNOWN"
    assert len(engine_inputs) == 4
    assert all(tuple(projection.quotes) == ("000002",) for _, projection in engine_inputs)
    assert all(
        projection.missing_symbols == ("000001", "000003", "000004")
        for _, projection in engine_inputs
    )


@pytest.mark.parametrize(
    ("source_milliseconds", "expected_eligible"),
    ((0, True), (197, True), (999, True), (1_000, False)),
)
def test_event_time_cutoff_truncates_subseconds(
    source_milliseconds, expected_eligible
):
    observed_at = datetime(
        2026, 9, 15, 9, 32, 10, 900_000, tzinfo=SHANGHAI
    )
    source_at = datetime(
        2026,
        9,
        15,
        9,
        32,
        10 + source_milliseconds // 1_000,
        (source_milliseconds % 1_000) * 1_000,
        tzinfo=SHANGHAI,
    )
    row = _row(
        "000001",
        price="11850",
        source_time_ms=int(source_at.timestamp() * 1000),
    )

    result = _run_pass(
        [row],
        trade_date="2026-09-15",
        observed_at=observed_at,
        stale_after_ms=60_000,
        symbols=("000001",),
        shuffled=False,
    )

    assert result["engine_input"]["sample_quote_count"] == int(expected_eligible)
    assert ("future_ts" in result["projection"]["field_error_counts"]) is (
        not expected_eligible
    )


def test_report_records_requested_and_effective_whole_second_cutoffs(
    monkeypatch, tmp_path, capsys
):
    observed_at = datetime(
        2026, 9, 15, 9, 32, 10, 900_000, tzinfo=SHANGHAI
    )
    source_at = datetime(2026, 9, 15, 9, 32, 10, 999_000, tzinfo=SHANGHAI)
    capture = tmp_path / "same-second-q2.jsonl"
    capture.write_text(
        json.dumps(
            _row(
                "000001",
                price="11850",
                source_time_ms=int(source_at.timestamp() * 1000),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "report.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_task008_replay_opening_validation.py",
            "--input",
            str(capture),
            "--trade-date",
            "2026-09-15",
            "--observed-at",
            observed_at.isoformat(),
            "--stale-after-ms",
            "10000",
            "--symbols",
            "000001",
            "--output",
            str(output),
        ],
    )

    assert main() == 0
    capsys.readouterr()
    report = json.loads(output.read_text(encoding="utf-8"))

    assert report["requested_observed_at"] == "2026-09-15T09:32:10.900000+08:00"
    assert report["observed_at"] == "2026-09-15T09:32:10+08:00"
    assert report["time_precision_policy"] == "TRUNCATE_TO_WHOLE_SECONDS"
    assert report["ordered"]["engine_input"]["sample_quote_count"] == 1


def test_stale_projection_does_not_mean_replay_execution_was_incomplete(
    monkeypatch, tmp_path, capsys
):
    observed_at = datetime(2026, 9, 15, 9, 32, 10, tzinfo=SHANGHAI)
    source_time_ms = int((observed_at - timedelta(seconds=15)).timestamp() * 1000)
    fresh_source_time_ms = int((observed_at - timedelta(seconds=1)).timestamp() * 1000)
    capture = tmp_path / "stale-q2.jsonl"
    capture.write_text(
        "\n".join(
            (
                json.dumps(_row("000001", price="11850", source_time_ms=source_time_ms)),
                json.dumps(
                    _row("000002", price="3050", source_time_ms=fresh_source_time_ms)
                ),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "report.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_task008_replay_opening_validation.py",
            "--input",
            str(capture),
            "--trade-date",
            "2026-09-15",
            "--observed-at",
            observed_at.isoformat(),
            "--stale-after-ms",
            "10000",
            "--symbols",
            "000001,000002",
            "--output",
            str(output),
        ],
    )

    assert main() == 0
    capsys.readouterr()
    report = json.loads(output.read_text(encoding="utf-8"))

    assert report["contract_version"] == "Task008ReplayOpeningValidationV4"
    assert report["replay_execution_status"] == "COMPLETE"
    assert report["replay_determinism_status"] == "PASS"
    assert report["exit_code"] == 0
    assert report["projection_quality_status"] == "PARTIAL"
    assert report["ordered"]["projection"]["stale_count"] == 1
    assert "replay_status" not in report
    assert report["normal_opening_pass"] == "UNPROVEN"


def test_duplicate_symbols_are_rejected_instead_of_silently_overwritten():
    observed_at = datetime(2026, 9, 15, 9, 32, 10, tzinfo=SHANGHAI)
    rows = [
        _row("000001", price="11850", source_time_ms=1789479130000),
        _row("000001", price="11860", source_time_ms=1789479131000),
    ]

    with pytest.raises(ValueError, match="duplicate symbol"):
        _projection(
            rows,
            trade_date="2026-09-15",
            observed_at=observed_at,
            stale_after_ms=10_000,
        )


def test_no_engine_symbol_match_is_not_reported_as_deterministic(
    monkeypatch, tmp_path, capsys
):
    observed_at = datetime(2026, 9, 15, 9, 32, 10, tzinfo=SHANGHAI)
    source_time_ms = int((observed_at - timedelta(seconds=1)).timestamp() * 1000)
    capture = tmp_path / "q2.jsonl"
    capture.write_text(
        json.dumps(_row("000001", price="11850", source_time_ms=source_time_ms)) + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "report.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_task008_replay_opening_validation.py",
            "--input",
            str(capture),
            "--trade-date",
            "2026-09-15",
            "--observed-at",
            observed_at.isoformat(),
            "--stale-after-ms",
            "10000",
            "--symbols",
            "999999",
            "--output",
            str(output),
        ],
    )

    assert main() == 3
    capsys.readouterr()
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["replay_execution_status"] == "COMPLETE"
    assert report["replay_determinism_status"] == "NOT_COMPARABLE"
    assert report["exit_code"] == 3
    assert report["engine_comparison_symbols"] == []


def test_future_only_sample_is_not_a_vacuous_engine_determinism_pass(
    monkeypatch, tmp_path, capsys
):
    observed_at = datetime(2026, 9, 15, 9, 32, 10, tzinfo=SHANGHAI)
    source_time_ms = int((observed_at + timedelta(seconds=1)).timestamp() * 1000)
    capture = tmp_path / "future-q2.jsonl"
    capture.write_text(
        json.dumps(_row("000001", price="11850", source_time_ms=source_time_ms))
        + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "report.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_task008_replay_opening_validation.py",
            "--input",
            str(capture),
            "--trade-date",
            "2026-09-15",
            "--observed-at",
            observed_at.isoformat(),
            "--stale-after-ms",
            "10000",
            "--symbols",
            "000001",
            "--output",
            str(output),
        ],
    )

    assert main() == 3
    capsys.readouterr()
    report = json.loads(output.read_text(encoding="utf-8"))

    assert report["projection_determinism_status"] == "PASS"
    assert report["replay_determinism_status"] == "NOT_COMPARABLE"
    assert report["engine_comparison_status"] == "NOT_COMPARABLE"
    assert report["exit_code"] == 3
    assert report["engine_sample_quote_symbols"] == []
    assert report["ordered"]["engine"]["000001"]["fact_status"] == "MISSING"


def test_input_hash_is_for_the_bytes_loaded_before_replay(
    monkeypatch, tmp_path, capsys
):
    observed_at = datetime(2026, 9, 15, 9, 32, 10, tzinfo=SHANGHAI)
    source_time_ms = int((observed_at - timedelta(seconds=1)).timestamp() * 1000)
    capture = tmp_path / "q2.jsonl"
    original_bytes = (
        json.dumps(_row("000001", price="11850", source_time_ms=source_time_ms)) + "\n"
    ).encode("utf-8")
    capture.write_bytes(original_bytes)
    output = tmp_path / "report.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_task008_replay_opening_validation.py",
            "--input",
            str(capture),
            "--trade-date",
            "2026-09-15",
            "--observed-at",
            observed_at.isoformat(),
            "--stale-after-ms",
            "10000",
            "--symbols",
            "000001",
            "--output",
            str(output),
        ],
    )
    original_run_pass = replay_tool._run_pass
    calls = 0

    def rewrite_capture_after_first_pass(*args, **kwargs):
        nonlocal calls
        result = original_run_pass(*args, **kwargs)
        calls += 1
        if calls == 1:
            capture.write_text("changed after input load\n", encoding="utf-8")
        return result

    monkeypatch.setattr(replay_tool, "_run_pass", rewrite_capture_after_first_pass)

    assert main() == 0
    capsys.readouterr()
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["input_sha256"] == hashlib.sha256(original_bytes).hexdigest()


def test_determinism_mismatch_is_written_and_returns_failure(
    monkeypatch, tmp_path, capsys
):
    observed_at = datetime(2026, 9, 15, 9, 32, 10, tzinfo=SHANGHAI)
    source_time_ms = int((observed_at - timedelta(seconds=1)).timestamp() * 1000)
    capture = tmp_path / "q2.jsonl"
    capture.write_text(
        json.dumps(_row("000001", price="11850", source_time_ms=source_time_ms)) + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "report.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_task008_replay_opening_validation.py",
            "--input",
            str(capture),
            "--trade-date",
            "2026-09-15",
            "--observed-at",
            observed_at.isoformat(),
            "--stale-after-ms",
            "10000",
            "--symbols",
            "000001",
            "--output",
            str(output),
        ],
    )
    original_run_pass = replay_tool._run_pass

    def change_shuffled_engine_result(*args, **kwargs):
        result = original_run_pass(*args, **kwargs)
        if kwargs["shuffled"]:
            result["engine"]["000001"]["content_hash"] = "different"
        return result

    monkeypatch.setattr(replay_tool, "_run_pass", change_shuffled_engine_result)

    assert main() == 2
    capsys.readouterr()
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["replay_execution_status"] == "COMPLETE"
    assert report["replay_determinism_status"] == "MISMATCH"
    assert report["engine_comparison_status"] == "MISMATCH"
    assert report["exit_code"] == 2


def test_loader_accepts_frozen_redis_projection_capture(tmp_path):
    capture = tmp_path / "redis_q2_capture.json"
    capture.write_text(
        json.dumps(
            {
                "projection": {
                    "quotes": {
                        "000001": {
                            "price_milli": 11850,
                            "pre_close_milli": 11740,
                            "amount_yuan": 876070976,
                            "source_record_time_ms": 1789369200000,
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    rows = _load_rows(capture)

    assert rows == [
        {
            "symbol": "000001",
            "px": 11850,
            "pc": 11740,
            "amt": 876070976,
            "ts": 1789369200000,
        }
    ]
