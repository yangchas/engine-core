from .spec import DatasetSpec

SPEC = DatasetSpec('plate_stock_index', 'td_plate_members_snapshot', 'on_demand',
                   redis_ttl_trade_days=5, primary_backend='redis',
                   dependencies=('trade_calendar', 'plate_members'), kind='day_refetchable',
                   source_dependency='td', empty_is_valid=False)
