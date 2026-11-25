from .spec import DatasetSpec

SPEC = DatasetSpec("hot_plates", "kaipan_hot_plates", "09:15-15:00_selected_points",
                primary_backend="redis", retention_policy="5_trade_days",
                redis_ttl_trade_days=5, max_redis_payload_bytes=524288,
                source_dependency="kaipan", fetch_budget_scope="kaipan",
                key_fields=("plate_code", "date", "captured_at"))
