from .spec import DatasetSpec

SPEC = DatasetSpec("hot_plate_minutes", "kaipan_getHisPlateMins", "after_daily_history_available",
                hot_view=False, max_redis_payload_bytes=4194304,
                source_dependency="kaipan", fetch_budget_scope="kaipan",
                key_fields=("plate_code", "time"))
