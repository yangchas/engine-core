"""TD row -> Q2 raw mapping pinned to real 2026-10-09 artifacts."""

import json
from datetime import datetime, timezone
from pathlib import Path

from engine_core import build_q2_projection
from engine_core.replay import TDEventV1

FIXTURE = Path(__file__).parent / "fixtures/facts/td_frame_q2_vol_20261009.json"


def _rows():
    return json.loads(FIXTURE.read_text())["rows"]


def _raw(row):
    return TDEventV1.from_mapping(
        {**row["td"], "symbol": row["symbol"]}
    ).to_q2_raw()


def test_td_row_maps_shared_cumulative_fields_equal_to_live_q2():
    for row in _rows():
        q2 = row["q2"]
        q2_shared = {key: q2[key] for key in ("px", "pc", "amt", "vol", "ts")}
        assert _raw(row) == q2_shared, row["symbol"]


def test_event_level_fields_are_not_derived_from_td_rows():
    for row in _rows():
        assert not {"iv", "ia", "ln"} & set(_raw(row)), row["symbol"]
    differing = [
        row["symbol"]
        for row in _rows()
        if row["q2"]["iv"] != row["td"]["inst_vol"]
    ]
    assert differing == ["002709", "002712"]


def test_explicit_zero_volume_is_distinct_from_missing_volume():
    row = _rows()[0]
    explicit_zero = {**row["td"], "symbol": row["symbol"], "vol_units": 0}
    missing = {**row["td"], "symbol": row["symbol"]}
    del missing["vol_units"]

    assert TDEventV1.from_mapping(explicit_zero).to_q2_raw()["vol"] == 0
    assert "vol" not in TDEventV1.from_mapping(missing).to_q2_raw()


def test_projection_keeps_volume_and_leaves_instants_missing():
    row = _rows()[0]
    observed = datetime.fromtimestamp(row["q2"]["ts"] / 1000, timezone.utc)
    projection = build_q2_projection(
        "2026-10-09", observed, (row["symbol"],), {row["symbol"]: _raw(row)}
    )
    quote = projection.quotes[row["symbol"]]
    assert quote.volume_lots == row["q2"]["vol"]
    assert quote.instant_volume_lots is None
    assert quote.instant_amount_yuan is None
