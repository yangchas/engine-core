"""C611 builder regression on captured Kaipan upstream responses."""
from __future__ import annotations

import json
from pathlib import Path

from engine_core.data_pipeline.limit_up_detail import _reason_for_day, build_limit_up_detail_rows
from engine_core.data_pipeline.limit_up_detail_td import _meta_ts
from engine_core.data_pipeline.td_sink import trade_day_ts_us

ROOT = Path(__file__).resolve().parents[1]
REAL = ROOT / "tests/fixtures/real"


def _capture(name: str) -> tuple[dict, dict]:
    provenance = json.loads((REAL / "PROVENANCE.json").read_text(encoding="utf-8"))[name]
    payload = json.loads((REAL / name).read_text(encoding="utf-8"))
    return payload, provenance


def test_real_kaipan_raw_detail_preserves_uncertain_fields_and_reason_lineage():
    pool_name = "kaipan_pool_2026-10-23_L1_0537.json"
    reason_name = "kaipan_ban_reasons_2026-10-29_002982_st200_0537.json"
    pool, pool_meta = _capture(pool_name)
    reasons, reason_meta = _capture(reason_name)

    rows = build_limit_up_detail_rows(
        "2026-10-23", [pool], {"002982": reasons},
        captured_at=pool_meta["fetched_at"], reason_captured_at={"002982": reason_meta["fetched_at"]},
    )
    from engine_core.guardian.limit_up_detail_d1 import _rows_by_symbol
    assert _rows_by_symbol(rows)["002982"]["symbol"] == "002982"
    row = next(item for item in rows if item["symbol"] == "002982")

    reason = next(item for item in reasons["List"]
                  if item.get("Date") == "2026-10-23" and str(item.get("Type")) == "0")
    assert row["trade_date"] == "2026-10-23"
    assert row["name"] == "湘佳股份"
    assert row["lb_days"] == 1
    assert row["price"] == 13.28
    assert row["pct_chg"] == 10.02
    assert row["trade_amount_yuan"] == 45206713
    assert row["limit_time_epoch"] == 1792718700
    assert row["first_seal_time"] == "09:25:00"
    assert row["kp_09_raw"] == 41177542
    assert row["main_buy_yuan"] is None and row["turnover_yuan"] is None
    assert row["reason_text_raw"] == reason["Reason"]
    assert row["theme"] == reason["Reason"].replace("；", ";").split(";", 1)[0]
    assert row["parser_version"]
    assert row["main_reason_plate_code"] == "801573"
    assert json.loads(row["reason_zscodes_json"]) == ["801573", "801339"]
    assert row["reason_source_date"] == "2026-10-23"


def test_limit_up_detail_versions_get_distinct_td_timestamps():
    existing = [{"version": "sha256-old", "ts": trade_day_ts_us("2026-10-28")}]
    captured = "2026-10-29T12:00:00+08:00"
    new_ts = _meta_ts("2026-10-28", "sha256-new", captured, existing)

    assert new_ts > existing[0]["ts"]
    assert _meta_ts("2026-10-28", "sha256-old", captured, existing) == existing[0]["ts"]


def test_real_reason_history_is_labeled_when_only_an_earlier_type0_record_exists():
    reason_name = "kaipan_ban_reasons_2026-10-29_002982_st200_0537.json"
    reasons, _ = _capture(reason_name)
    row = _reason_for_day("002982", "2026-10-24", reasons)

    assert row["status"] == "HISTORICAL_ATTRIBUTION"
    assert row["reason_source_date"] == "2026-10-23"
    assert row["reason_text_raw"]
