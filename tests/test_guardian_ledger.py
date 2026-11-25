"""Ledger audit behavior using minimal contract-shaped records."""
import json

from engine_core.guardian.ledger_degraded import degraded_report


def test_degraded_report_includes_missing_reason_and_legacy_records(tmp_path):
    path = tmp_path / "ledger.jsonl"
    records = [
        {"type": "ATTEMPT_END", "data": {"instance": "a", "attempt": 1, "reply": {"status": "degraded", "reason": "partial", "error_code": "DATA", "detail": {"rows_count": 2}}}},
        {"type": "INSTANCE_STATE", "data": {"instance": "a", "event_id": "event", "state": {"status": "SUCCEEDED_DEGRADED", "attempts": 1}}},
        {"type": "ATTEMPT_END", "data": {"instance": "b", "attempt": 2}},
        {"type": "INSTANCE_STATE", "data": {"instance": "b", "event_id": "event", "state": {"status": "SUCCEEDED_DEGRADED", "attempts": 2}}},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in records))
    rows = degraded_report(path)
    assert rows[0] == {"event": "event", "instance": "a", "attempt": 1, "reason": "partial", "reason_missing": False, "error_code": "DATA", "detail": {"rows_count": 2}}
    assert rows[1]["reason_missing"] and rows[1]["reason"] is None
