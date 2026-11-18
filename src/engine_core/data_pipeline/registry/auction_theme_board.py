"""Pure assembly inputs: auction_0925, auction_0924_meta, prior_0925_by_date,
plate_map, caps, yesterday_pool, yesterday_reasons, reason_history, seal, names,
listings, plate_names, trading_dates. The caller supplies availability metadata.
"""
from .spec import DatasetSpec, FieldSpec

SPEC = DatasetSpec(
    'auction_theme_board', 'derived', 'on_demand', redis_ttl_trade_days=5,
    primary_backend='redis', dependencies=('trade_calendar',),
    kind='day_refetchable', source_dependency='independent_provider',
    fields=tuple(FieldSpec(name, str, False, 'text', {}) for name in
                 ('digest_md', 'board_md', 'summary_json', 'buckets_json')),
)
