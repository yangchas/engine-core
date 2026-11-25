from .spec import DatasetSpec

SPEC = DatasetSpec("stock_static", "wencai_independent_client", "low_frequency_incremental",
                redis_ttl_seconds=3600, max_redis_payload_bytes=65536,
                dependencies=("symbol_universe",), source_dependency="independent_wencai",
                fetch_budget_scope="wencai", key_fields=("symbol",))
