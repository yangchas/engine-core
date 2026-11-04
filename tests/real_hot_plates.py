"""Real Kaipan hot-plate rows for tests: the recorded raw getHisPlates response parsed by the real KaipanProvider."""
from __future__ import annotations

import json
from pathlib import Path

from engine_core.data_pipeline.providers.kaipan_provider import KaipanProvider

RAW = Path(__file__).parent / "fixtures" / "real" / "kaipan_hot_plates_2026-10-23_1441.json"


class _Replay:
    def __init__(self, plates):
        self.plates = plates

    def get_his_plates(self, day, index, size):
        return self.plates


def real_plate_rows(captured_at: str, *, trade_date: str | None = None, start: int = 0, count: int = 1) -> list[dict]:
    """Rows [start:start+count] of the recorded response. ``captured_at`` (and the matching available_at) is the
    scenario's capture instant, which the PIT tests choose; the plate fields are the recorded ones."""
    raw = json.loads(RAW.read_text(encoding="utf-8"))
    rows = KaipanProvider(_Replay(raw)).hot_plates(raw["Day"][0], max_pages=1)["rows"][start:start + count]
    return [{**row, "trade_date": trade_date or row["trade_date"], "captured_at": captured_at,
             "available_at": captured_at} for row in rows]
