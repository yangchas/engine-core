from .spec import DatasetSpec

SPEC = DatasetSpec("symbol_universe", "baostock_query_all_stock", "08:30_before_premarket",
                hot_view=False, empty_is_valid=False, key_fields=("symbol",),
                required_fields=("symbol", "name", "trade_status", "universe_asof"))
