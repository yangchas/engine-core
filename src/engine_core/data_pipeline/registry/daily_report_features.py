from .spec import DatasetSpec

SPEC = DatasetSpec("daily_report_features", "core_derived", "after_daily_kline_close",
                max_redis_payload_bytes=524288, dependencies=("daily_kline",),
                source_dependency="core_data", key_fields=("symbol", "date"))
