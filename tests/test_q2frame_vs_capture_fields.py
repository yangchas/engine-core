"""Synthetic-data tests for the Q2Frame vs sealed-capture field comparison runner."""

import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "q2frame_vs_capture",
    Path(__file__).parents[1] / "examples" / "run_q2frame_vs_capture_fields.py",
)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

TS = 1791509964000


def _update(symbol, ts, px=100, **extra):
    row = {"symbol": symbol, "px": px, "pc": 100, "amt": 1000, "vol": 10, "ts": ts, "iv": 0,
           "ia": 0, "ln": 0, "ph": 2, "ls": 0, "mk": "sz"}
    row.update(extra)
    return row


def _frames(path, frames):
    with path.open("w") as handle:
        for seq, (ts, updates) in enumerate(frames, start=1):
            handle.write(json.dumps({"version": "Q2FrameV1", "seq_no": seq, "logical_ts_ms": ts,
                                     "phase": 2, "q2_updates": updates}) + "\n")


def _capture(path, rows):
    reads = [{"key": "q2:active:20261009", "operation": "smembers", "value": [r["symbol"] for r in rows]}]
    for row in rows:
        value = {k: str(v) for k, v in row.items() if k != "symbol"}
        reads.append({"key": "q2:" + row["symbol"], "operation": "hgetall", "value": value})
    path.write_text(json.dumps({"reads": reads, "read_started_at": "x", "read_completed_at": "y"}))


def test_same_ts_newer_older_and_value_difference_are_classified_separately(tmp_path):
    frames = tmp_path / "f.jsonl"
    _frames(frames, [
        (TS, [_update("000001", TS), _update("000002", TS, px=201), _update("000003", TS + 3000),
              _update("000004", TS - 3000)]),
    ])
    capture = tmp_path / "c.json"
    _capture(capture, [_update("000001", TS), _update("000002", TS, px=200),
                       _update("000003", TS), _update("000004", TS)])
    out = runner.run(q2frame=frames, capture_path=capture, trade_date="2026-10-09", out_dir=tmp_path / "o")
    comparison = out["comparison"]
    assert comparison["relation_counts"] == {
        "SAME_TS": 2, "REPLAY_SOURCE_NEWER": 1, "REPLAY_SOURCE_OLDER": 1,
    }
    assert comparison["relation_sum_equals_symbols"] is True
    assert comparison["field_status_by_relation"]["px"]["SAME_TS:MISMATCH"] == 1
    assert comparison["field_status_counts"]["ts"] == {"MATCH": 2, "MISMATCH": 2}
    assert comparison["projection_deterministic_on_repeat"] is True


def test_field_missing_on_one_side_is_unknown_not_zero(tmp_path):
    frames = tmp_path / "f.jsonl"
    _frames(frames, [(TS, [_update("000001", TS, a25=7000)])])
    capture = tmp_path / "c.json"
    row = _update("000001", TS)
    _capture(capture, [row])
    out = runner.run(q2frame=frames, capture_path=capture, trade_date="2026-10-09", out_dir=tmp_path / "o")
    assert out["comparison"]["field_status_counts"]["a25"] == {"UNKNOWN": 1}


def test_symbol_without_replay_state_is_listed_not_dropped(tmp_path):
    frames = tmp_path / "f.jsonl"
    _frames(frames, [(TS, [_update("000001", TS)])])
    capture = tmp_path / "c.json"
    _capture(capture, [_update("000001", TS), _update("000009", TS)])
    out = runner.run(q2frame=frames, capture_path=capture, trade_date="2026-10-09", out_dir=tmp_path / "o")
    comparison = out["comparison"]
    assert comparison["relation_counts"]["NO_REPLAY_STATE"] == 1
    assert comparison["no_replay_state_symbols"] == ["000009"]
