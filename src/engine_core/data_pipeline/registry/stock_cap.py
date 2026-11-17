from .spec import DatasetSpec, FieldSpec

SPEC = DatasetSpec(
    'stock_cap', 'wencai', 'on_demand', redis_ttl_trade_days=5,
    primary_backend='redis', dependencies=('trade_calendar', 'stock_static'),
    kind='day_refetchable', source_dependency='independent_provider',
    fields=(FieldSpec('symbol', str, False, 'symbol', {}),
            FieldSpec('float_cap_yuan', float, True, 'yuan', {'wencai': 'yuan'}),
            FieldSpec('total_cap_yuan', float, True, 'yuan', {'wencai': 'yuan'}),
            FieldSpec('a_total_cap_yuan', float, True, 'yuan', {'wencai': 'yuan'}),
            FieldSpec('cap_as_of', str, False, 'date', {})),
)
