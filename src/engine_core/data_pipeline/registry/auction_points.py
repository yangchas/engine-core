from .spec import DatasetSpec

SPEC = DatasetSpec("auction_points", "t1_v2_redis_market_auction+a2", "09:20/09:24/09:25_capture",
                redis_ttl_seconds=604800, max_redis_payload_bytes=2097152,
                source_dependency="legacy_shared_redis", key_fields=("trade_date", "tag", "symbol", "source_key"),
                required_fields=("trade_date", "tag", "symbol", "source_key", "point_ts"))
