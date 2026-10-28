"""C584: degraded daily_kline writer emits volume in shares, matching manifest field_units.

q2 rows come from the real captured Redis hashes (tests/fixtures/real/c526); the expectation is
computed from the raw `vol` (hands) x 100, independent of the production conversion code.
"""
from __future__ import annotations

from datetime import datetime

from test_daily_kline_manifest_contract import DAY, _Redis, _q2_hashes, _seed_universe

from engine_core.daily_kline_degraded import capture, wencai_rows_to_canonical_units


def _raw_vol(item):
    value = item["value"]
    for key in ("vol", b"vol"):
        if key in value:
            raw = value[key]
            return float(raw.decode() if isinstance(raw, bytes) else raw)
    raise AssertionError(f"no vol in {list(value)[:5]}")


def test_degraded_q2_volume_is_shares_and_manifest_agrees(tmp_path):
    hashes = _q2_hashes()
    _seed_universe(tmp_path, DAY, symbols=hashes)
    out = capture(DAY, artifact_root=tmp_path, redis_client=_Redis(hashes),
                  now=datetime.fromisoformat(DAY + "T15:10:00+08:00"), write=True)
    manifest = out["manifest"]
    assert manifest["field_units"]["volume"] == "shares"
    assert manifest["quality"]["field_units"]["volume"] == "shares"
    checked = 0
    for row in out["rows"]:
        if row.get("volume") is None:
            continue
        assert row["volume"] == _raw_vol(hashes[row["symbol"]]) * 100
        if row.get("amount_yuan") and row.get("low") and row.get("high"):
            vwap = row["amount_yuan"] / row["volume"]
            assert row["low"] * 0.98 <= vwap <= row["high"] * 1.02
        checked += 1
    assert checked >= 50


def test_wencai_volume_converted_by_declared_unit():
    rows = [{"symbol": "000001", "volume": 12.0, "amount_yuan": 3.0}]
    out = wencai_rows_to_canonical_units(rows, {"volume": "hands", "amount_yuan": "wan_yuan"})
    assert out[0]["volume"] == 1200.0 and out[0]["amount_yuan"] == 30000.0
    shares = wencai_rows_to_canonical_units(rows, {"volume": "shares", "amount_yuan": "yuan"})
    assert shares[0]["volume"] == 12.0
    unknown = wencai_rows_to_canonical_units(rows, {"volume": "UNKNOWN"})
    assert unknown[0]["volume"] is None
