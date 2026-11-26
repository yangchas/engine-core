"""Explicit read-only TDengine boundary; importing this module requires no taos."""
from datetime import date

class TDReader:
    TABLES = {'daily_kline': ('core_data.daily_kline_v1', 'ts'),
              'daily_dde': ('core_data.daily_dde_v1', 'ts'),
              'chip': ('core_data.chip_indicators_v1', 'ts'),
              'price_volume': ('core_data.price_volume_indicators_v1', 'ts'),
              'ticks': ('market_data1.stock_tick_v2', 'ts'),
              'limit_up_detail': ('core_data.limit_up_detail_v1', 'trade_date')}
    def __init__(self, **connect_options):
        self.connect_options = connect_options
    def read(self, dataset, start_date, end_date):
        table, column = self.TABLES[dataset]
        start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
        if end < start: raise ValueError('end_date precedes start_date')
        import taos
        conn = taos.connect(**self.connect_options)
        try:
            cur = conn.cursor()
            try:
                cur.execute(f"SELECT * FROM {table} WHERE {column} >= '{start.isoformat()} 00:00:00' AND {column} < '{(end.fromordinal(end.toordinal()+1)).isoformat()} 00:00:00'")
                names = [item[0] for item in cur.description]
                return [dict(zip(names, row)) for row in cur.fetchall()]
            finally:
                cur.close()
        finally:
            conn.close()
