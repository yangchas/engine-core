from .spec import DatasetSpec

SPEC = DatasetSpec("hot_plates", "kaipan_hot_plates", "09:15-15:00_selected_points",
                redis_ttl_seconds=4 * 86400, max_redis_payload_bytes=524288,
                source_dependency="kaipan", fetch_budget_scope="kaipan",
                key_fields=("plate_code", "date", "captured_at"))
