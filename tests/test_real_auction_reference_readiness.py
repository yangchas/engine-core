import json
from datetime import datetime, timezone

import pytest

from engine_core import (
    build_calendar_snapshot,
    canonical_hot_plates_payload_hash,
    canonical_previous_day_limit_pool_payload_hash,
    local_datetime_ms,
)
from examples.run_real_auction_reference_readiness import (
    run_real_auction_reference_readiness,
)


TRADE_DATE = "2026-09-16"
PREVIOUS_DATE = "2026-09-15"
NOW = local_datetime_ms(TRADE_DATE, "09:19:00")


class ReadOnlyRedis:
    def __init__(self):
        available = NOW - 1
        limit_row = {
            "trade_date": PREVIOUS_DATE,
            "symbol": "000001",
            "name": "sample",
            "lb_days": 1,
            "plate": "bank",
            "seal_time": "09:31:00",
            "turnover": 10.0,
            "close_pct": 10.0,
            "source": "kaipan",
        }
        hot_row = {
            "trade_date": TRADE_DATE,
            "plate_name": "bank",
            "rank": 1,
            "strength": 10.0,
            "hot": 10.0,
            "change_pct": 1.0,
            "net_inflow_yi": 1.0,
            "source": "kaipan",
        }
        self.hashes = {
            "cache:yest_limit_pool:" + PREVIOUS_DATE: {
                "000001": json.dumps(limit_row)
            },
            "cache:hot_plates:" + TRADE_DATE: {
                "bank": json.dumps(hot_row)
            },
            "q2:000001": {
                "px": "1000",
                "pc": "990",
                "amt": "100000",
                "ts": str(NOW),
                "mk": "sz",
            },
        }
        self.strings = {
            "cache:yest_limit_pool_meta:" + PREVIOUS_DATE: json.dumps(
                {
                    "schema_version": "PreviousDayLimitPoolV1",
                    "available_at_ms": available,
                    "field_units": {
                        "lb_days": "boards",
                        "turnover": "percent",
                        "close_pct": "percent",
                    },
                    "payload_sha256": canonical_previous_day_limit_pool_payload_hash((limit_row,)),
                }
            ),
            "cache:hot_plates_meta:" + TRADE_DATE: json.dumps(
                {
                    "schema_version": "HotPlatesV1",
                    "available_at_ms": available,
                    "field_units": {
                        "rank": "ordinal",
                        "strength": "score",
                        "hot": "score",
                        "change_pct": "percent",
                        "net_inflow_yi": "yi",
                    },
                    "payload_sha256": canonical_hot_plates_payload_hash((hot_row,)),
                }
            ),
        }

    def type(self, key):
        return "hash" if key in self.hashes else "none"

    def hlen(self, key):
        return len(self.hashes.get(key, {}))

    def hscan_iter(self, key):
        return iter(self.hashes.get(key, {}).items())

    def get(self, key):
        return self.strings.get(key)

    def smembers(self, key):
        return {"000001"} if key == "q2:active:20260916" else set()

    def hgetall(self, key):
        return self.hashes.get(key, {})


def test_real_reference_runner_uses_read_only_sources_and_truthful_statuses():
    calendar = build_calendar_snapshot(
        (PREVIOUS_DATE, TRADE_DATE),
        version="real-reference-runner-test-v1",
        declared_valid_from=TRADE_DATE,
        declared_valid_to=TRADE_DATE,
        source_guard_valid_from=PREVIOUS_DATE,
        source_guard_valid_to=TRADE_DATE,
    )
    result = run_real_auction_reference_readiness(
        client=ReadOnlyRedis(),
        trade_date=TRADE_DATE,
        calendar=calendar,
        observed_at=datetime.fromtimestamp(NOW / 1000, tz=timezone.utc),
        symbols=("000001",),
        stale_after_ms=60_000,
        td_kwargs={},
        fetch_td_rows_override=lambda date, symbols: [
            {"symbol": "000001", "close": 10.0, "amount": 1000, "volume": 10}
        ],
    )

    assert result["read_only"] is True
    assert result["previous_trade_date"] == PREVIOUS_DATE
    assert result["q2"]["status"] == "READY"
    # TD daily_kline has no verified historical publication timestamp.
    assert result["reference_results"]["previous_day_stats"]["status"] == "UNAVAILABLE"
    assert result["reference_results"]["previous_day_limit_pool"]["status"] == "READY"
    assert result["reference_results"]["hot_plates"]["status"] == "READY"
    assert result["readiness"]["status"] == "PARTIAL"
    assert "Redis" in result["side_effect_boundary"]


def test_real_reference_runner_rejects_unbounded_td_probe():
    calendar = build_calendar_snapshot(
        (PREVIOUS_DATE, TRADE_DATE),
        version="real-reference-runner-test-v1",
        declared_valid_from=TRADE_DATE,
        declared_valid_to=TRADE_DATE,
        source_guard_valid_from=PREVIOUS_DATE,
        source_guard_valid_to=TRADE_DATE,
    )
    with pytest.raises(ValueError, match="bounded symbol set"):
        run_real_auction_reference_readiness(
            client=ReadOnlyRedis(),
            trade_date=TRADE_DATE,
            calendar=calendar,
            observed_at=datetime.fromtimestamp(NOW / 1000, tz=timezone.utc),
            symbols=(),
            stale_after_ms=60_000,
            td_kwargs={},
            fetch_td_rows_override=lambda date, symbols: [],
        )
