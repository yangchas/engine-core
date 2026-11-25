import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from engine_core import Q2FrameReplaySource, VirtualClock


FIXTURE = Path(__file__).parent / "fixtures/q2/q2frame_source_time_real_20260929.json"


def test_real_q2frame_keeps_symbol_source_time_separate_from_frame_clock():
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    source_meta = fixture["source"]
    update = fixture["q2_update"]
    canonical_update = json.dumps(
        update, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    assert hashlib.sha256(canonical_update).hexdigest() == source_meta[
        "q2_update_sha256"
    ]
    assert (
        source_meta["frame_logical_ts_ms"] - update["ts"]
        == 15 * 60 * 1000
    )
    frame = {
        "version": "Q2FrameV1",
        # Q2FrameReplaySource requires a session-local sequence starting at 1;
        # the captured producer sequence remains in fixture provenance.
        "seq_no": 1,
        "logical_ts_ms": source_meta["frame_logical_ts_ms"],
        "q2_updates": [update],
    }
    clock = VirtualClock(
        datetime.fromtimestamp(frame["logical_ts_ms"] / 1000, timezone.utc)
    )
    replay = Q2FrameReplaySource(
        fixture["trade_date"],
        (update["symbol"],),
        clock,
    )

    projection = replay.apply(frame)
    quote = projection.quotes[update["symbol"]]

    assert quote.source_record_time_ms == update["ts"]
    assert quote.source_record_time_ms != frame["logical_ts_ms"]
    assert projection.envelope.provenance.observed_at_ms == frame["logical_ts_ms"]
    assert projection.envelope.effective_time_ms == update["ts"]
    assert quote.price_milli == update["px"]
