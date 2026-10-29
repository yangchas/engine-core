from .spec import DatasetSpec

SPEC = DatasetSpec("hot_rank", "ths_hot_rank", "09:15-15:00_selected_points",
                redis_ttl_seconds=2 * 86400, max_redis_payload_bytes=262144,
                source_dependency="ths_independent_client_required", fetch_budget_scope="ths",
                key_fields=("symbol",))
