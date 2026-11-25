from .spec import DatasetSpec

SPEC = DatasetSpec("limit_reasons", "kaipan_getHisBans", "after_provider_ready_incremental",
                redis_ttl_seconds=4 * 86400, max_redis_payload_bytes=262144,
                source_dependency="kaipan", empty_is_valid=True,
                fetch_budget_scope="kaipan", key_fields=("date", "symbol"),
                primary_providers=("kaipan_getHisBans", "kaipan_getBanReasons(st=200)"),
                source_preference=("kaipan_getHisBans", "kaipan_getBanReasons(st=200)",
                                   "wencai_limit_up_pool.wc_reason_category", "historical_attribution"))
