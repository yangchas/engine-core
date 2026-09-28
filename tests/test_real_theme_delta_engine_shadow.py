from __future__ import annotations

import json
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from engine_core import infer_legacy_theme_delta_signal, read_redis_auction_projection
from examples.run_real_theme_delta_engine_shadow import (
    run_engine_shadow_from_real_inputs,
)


class _AuctionRedisFixture:
    """Small adapter fixture for Engine wiring; not real-market evidence."""

    def hgetall(self, key):
        tag = key.rsplit(":", 1)[-1]
        local = datetime.combine(
            datetime.fromisoformat("2026-09-28").date(),
            time(int(tag[:2]), int(tag[2:])),
            tzinfo=ZoneInfo("Asia/Shanghai"),
        )
        source_ms = int(local.astimezone(timezone.utc).timestamp() * 1000)
        row = {
            "symbol": "600519",
            "auction_amount_yuan": 1_200_000 if tag == "0920" else 1_300_000,
            "bid_amount_yuan": 700_000,
            "ask_amount_yuan": 200_000,
            "price": 100.0,
        }
        return {
            "meta": json.dumps({"tag": tag, "ts": source_ms, "n": 1}),
            "summary": json.dumps({"tag": tag}),
            "top_amount": json.dumps([row]),
        }


def test_frozen_real_input_engine_wiring_binds_theme_result_once_at_0925():
    trade_date = "2026-09-28"
    observed_at_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    projections = read_redis_auction_projection(
        _AuctionRedisFixture(),
        trade_date=trade_date,
        observed_at_ms=observed_at_ms,
        tags=("0920", "0924", "0925"),
    )
    signal = infer_legacy_theme_delta_signal(
        amount_delta_24_25=60_000_000.0,
        bid_amount_delta_24_25=0.0,
        change_pct_delta_avg=1.0,
        amount_ratio_avg=2.0,
    )
    facts = (
        {
            "theme_id": "fixture-theme",
            "symbol_count": 1,
            "amount_0925": 100.0,
            "amount_delta_24_25": 60_000_000.0,
            "amount_ratio_avg": 2.0,
            "bid_amount_delta_24_25": 0.0,
            "change_pct_delta_avg": 1.0,
            "positive_delta_count": 1,
            "evidence_refs": ("fixture://theme/fixture-theme",),
            "signal": signal,
        },
    )
    theme_shadow = {
        "status": "OBSERVED",
        "trade_date": trade_date,
        "observed_at_ms": observed_at_ms,
        "scope": "REDIS_TOP_AMOUNT_INTERSECTION",
        "projection_content_hashes": {
            projection.tag: projection.content_hash
            for projection in projections
            if projection.tag in ("0924", "0925")
        },
        "projection_evidence_hashes": {},
        "row_count": 1,
        "mapping_count": 1,
        "mapping_missing_count": 0,
        "facts": facts,
    }

    result = run_engine_shadow_from_real_inputs(
        projections=projections,
        theme_shadow=theme_shadow,
        trade_date=trade_date,
        symbol="600519",
        evaluation_base_ms=observed_at_ms + 2_000,
        redis_commands=(),
    )

    assert result["same_frozen_theme_cohort"] is True
    assert result["engine_matches_direct_theme_shadow"] is True
    assert result["engine_decision_status"] == "FACT_ONLY"
    assert result["processed_signals"] == 7
    assert result["strategy_result_count"] == 3
    assert result["pending_evaluations"] == 0
    assert result["historical_available_at"] == "UNKNOWN"
    assert result["full_market_coverage"] == "NOT_CLAIMED_TOP_AMOUNT_ONLY"


def test_engine_wiring_rejects_theme_facts_from_a_different_redis_cohort():
    trade_date = "2026-09-28"
    projections = read_redis_auction_projection(
        _AuctionRedisFixture(),
        trade_date=trade_date,
        observed_at_ms=1_790_579_196_000,
        tags=("0920", "0924", "0925"),
    )
    theme_shadow = {
        "status": "OBSERVED",
        "trade_date": trade_date,
        "observed_at_ms": 1_790_579_196_000,
        "projection_content_hashes": {"0924": "different", "0925": "different"},
        "facts": (),
    }

    try:
        run_engine_shadow_from_real_inputs(
            projections=projections,
            theme_shadow=theme_shadow,
            trade_date=trade_date,
            symbol="600519",
            evaluation_base_ms=1_790_579_198_000,
            redis_commands=(),
        )
    except ValueError as exc:
        assert "same Redis cohort" in str(exc)
    else:
        raise AssertionError("mismatched Redis cohorts must be rejected")
