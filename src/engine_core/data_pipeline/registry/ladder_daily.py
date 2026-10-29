from .spec import DatasetSpec

SPEC = DatasetSpec("ladder_daily", "core_derived", "after_daily_kline_and_pool",
                redis_ttl_seconds=7 * 86400, max_redis_payload_bytes=262144,
                dependencies=("daily_kline", "limit_up_pool", "trade_calendar"),
                key_fields=("symbol",))
