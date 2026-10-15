"""Dataset contracts. Sources are names only; fetchers are injected by callers."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class DatasetSpec:
    name: str
    source: str
    schedule: str
    primary_backend: str = "artifact"
    hot_view: bool = True
    redis_ttl_seconds: int = 604800
    max_redis_payload_bytes: int = 65536
    retention_policy: str = "immutable_versions"
    dependencies: tuple[str, ...] = ()
    point_in_time: bool = True
    source_dependency: str = "independent_provider"
    fetch_budget_scope: str = "default"
    empty_is_valid: bool = False
    key_fields: tuple[str, ...] = ("symbol",)
    required_fields: tuple[str, ...] = ()


_SPECS = (
    DatasetSpec("daily_kline", "baostock_then_wencai", "after_17:40_and_bounded_20:00_retry",
                max_redis_payload_bytes=65536, dependencies=("trade_calendar", "symbol_universe"),
                source_dependency="baostock_or_independent_wencai", key_fields=("symbol",),
                required_fields=("symbol",)),
    DatasetSpec("limit_up_pool", "kaipan_getHisBans", "08:50", max_redis_payload_bytes=262144,
                dependencies=("daily_kline", "trade_calendar"), source_dependency="kaipan",
                fetch_budget_scope="kaipan", key_fields=("symbol",)),
    DatasetSpec("hot_plates", "kaipan_hot_plates", "09:15-15:00_selected_points",
                redis_ttl_seconds=1800, max_redis_payload_bytes=524288,
                source_dependency="kaipan", fetch_budget_scope="kaipan", key_fields=("plate_code",)),
    DatasetSpec("hot_rank", "ths_hot_rank", "09:15-15:00_selected_points",
                redis_ttl_seconds=1800, max_redis_payload_bytes=262144,
                source_dependency="ths_independent_client_required", fetch_budget_scope="ths",
                key_fields=("symbol",)),
    DatasetSpec("plate_members", "kaipan_getHisPlateIds", "08:50_select_pit_weekly_refresh",
                max_redis_payload_bytes=65536, source_dependency="kaipan", empty_is_valid=True,
                fetch_budget_scope="kaipan", key_fields=("plate_code", "symbol")),
    DatasetSpec("limit_reasons", "kaipan_getBanReasons", "after_provider_ready_incremental",
                max_redis_payload_bytes=262144, source_dependency="kaipan", empty_is_valid=True,
                fetch_budget_scope="kaipan", key_fields=("symbol", "event_date", "type")),
    DatasetSpec("auction_baseline", "td_auction_snapshot_v2", "after_auction_input_ready",
                max_redis_payload_bytes=524288, dependencies=("auction_snapshot",),
                source_dependency="legacy_shared_td", key_fields=("trade_date", "symbol")),
    DatasetSpec("ladder_daily", "core_derived", "after_daily_kline_and_pool",
                max_redis_payload_bytes=262144, dependencies=("daily_kline", "limit_up_pool", "trade_calendar"),
                key_fields=("symbol",)),
    DatasetSpec("stock_static", "wencai_independent_client", "low_frequency_incremental",
                redis_ttl_seconds=3600, max_redis_payload_bytes=65536,
                dependencies=("symbol_universe",), source_dependency="independent_wencai",
                fetch_budget_scope="wencai", key_fields=("symbol",)),
    DatasetSpec("trade_calendar", "explicit_calendar_provider", "low_frequency", hot_view=False,
                empty_is_valid=False, key_fields=("date",)),
    DatasetSpec("symbol_universe", "explicit_universe_provider", "low_frequency", hot_view=False,
                empty_is_valid=False, key_fields=("symbol",)),
)

DATASETS = {spec.name: spec for spec in _SPECS}


def get_dataset(name: str) -> DatasetSpec:
    try:
        return DATASETS[name]
    except KeyError as exc:
        raise KeyError(f"unknown_dataset:{name}") from exc
