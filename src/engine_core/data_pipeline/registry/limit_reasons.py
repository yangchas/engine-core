from .spec import DatasetSpec

SPEC = DatasetSpec("limit_reasons", "kaipan_getBanReasons", "after_provider_ready_incremental",
                max_redis_payload_bytes=262144, source_dependency="kaipan", empty_is_valid=True,
                fetch_budget_scope="kaipan", key_fields=("symbol", "event_date", "type"))
