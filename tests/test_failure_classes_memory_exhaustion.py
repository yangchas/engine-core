# DATA: real:td/errors/query_memory_exhausted_0x073a.json; spec:timeline.json/schedule.json failure_classes
"""Every event that configures its own failure_classes must treat TD query-memory exhaustion as retryable."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
REAL_ERROR = json.loads((ROOT / "tests" / "fixtures" / "real" / "td" / "errors"
                         / "query_memory_exhausted_0x073a.json").read_text(encoding="utf-8"))["message"]
# the 2026-11-04 08:30 startup_0830 ledger text: prep step failed with the TD error inside its stderr tail
PREP_ERROR = ("prep_errors:plate_amount_baseline:COMMAND_FAILED:RuntimeError: "
              "plate_amount_baseline_auction_query 2026-09-18..2026-11-02: " + REAL_ERROR)
SCHEDULE = json.loads((ROOT / "src" / "engine_core" / "guardian" / "schedule.json").read_text(encoding="utf-8"))
CONFIGURED = [event for event in SCHEDULE["events"] if (event.get("args") or {}).get("failure_classes")]


def test_some_events_configure_their_own_failure_classes():
    assert len(CONFIGURED) >= 5          # startup, premarket_slice, baostock_probe, baostock_retry, fetch_item families


@pytest.mark.parametrize("event", CONFIGURED, ids=[event["event_id"] for event in CONFIGURED])
def test_tds_memory_exhaustion_is_retryable_for_every_event_with_its_own_classes(event):
    from examples import guardian_handler
    request = {"event_id": event["event_id"], "args": {"failure_classes": event["args"]["failure_classes"]}}
    code, reply = guardian_handler._failure_response(request, error=RuntimeError(PREP_ERROR))
    assert code == 75 and reply["retryable"] is True, event["event_id"]


def test_a_genuinely_deterministic_prep_error_is_still_final_for_those_events():
    from examples import guardian_handler
    event = next(e for e in CONFIGURED if e["event_id"] == "startup_0830")
    request = {"event_id": event["event_id"], "args": {"failure_classes": event["args"]["failure_classes"]}}
    code, _reply = guardian_handler._failure_response(request, error=RuntimeError("prep_critical_missing:symbol_universe"))
    assert code == 40
