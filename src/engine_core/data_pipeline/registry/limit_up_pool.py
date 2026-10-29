from .spec import DatasetSpec

SPEC = DatasetSpec("limit_up_pool", "kaipan_getHisBans", "08:50",
                redis_ttl_seconds=4 * 86400, max_redis_payload_bytes=262144,
                dependencies=("daily_kline", "trade_calendar"), source_dependency="kaipan",
                fetch_budget_scope="kaipan", key_fields=("symbol",))
