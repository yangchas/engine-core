from .spec import DatasetSpec

SPEC = DatasetSpec("stock_day_facts", "core_derived", "after_daily_kline",
                redis_ttl_seconds=7 * 86400, max_redis_payload_bytes=262144,
                dependencies=("daily_kline",),
                source_dependency="core_data", key_fields=("symbol",))
