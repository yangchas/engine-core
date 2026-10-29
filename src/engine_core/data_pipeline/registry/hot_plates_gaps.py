from .spec import DatasetSpec

SPEC = DatasetSpec("hot_plates_gaps", "guardian_gap_only", "after_hot_capture_deadline",
                   hot_view=False, redis_ttl_seconds=0, max_redis_payload_bytes=65536,
                   source_dependency="internal", fetch_budget_scope="guardian",
                   empty_is_valid=False, key_fields=("event_id",))
