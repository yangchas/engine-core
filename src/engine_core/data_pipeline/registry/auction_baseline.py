from .spec import DatasetSpec

SPEC = DatasetSpec("auction_baseline", "core_td_auction_snapshot_v2", "premarket_pit_64_sessions",
                max_redis_payload_bytes=65536, dependencies=("auction_snapshot",),
                source_dependency="read_only_td", key_fields=("trade_date", "symbol"),
                required_fields=("date", "row_kind"))
