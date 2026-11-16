from .spec import DatasetSpec

SPEC = DatasetSpec('auction_seal_facts', 'redis_auction_full+td_stock_tick_v2', 'on_demand',
                   redis_ttl_trade_days=5, primary_backend='redis',
                   dependencies=('trade_calendar', 'stock_static'), kind='day_refetchable',
                   source_dependency='redis_and_td', empty_is_valid=True)
