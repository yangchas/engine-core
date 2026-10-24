from .spec import DatasetSpec

SPEC = DatasetSpec("plate_members", "kaipan_getHisPlateIds", "08:50_select_pit_weekly_refresh",
                max_redis_payload_bytes=65536, source_dependency="kaipan", empty_is_valid=True,
                fetch_budget_scope="kaipan", key_fields=("plate_code", "symbol"))
