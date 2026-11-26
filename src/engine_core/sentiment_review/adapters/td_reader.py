"""Explicit read-only TDengine boundary; importing this module requires no taos."""
from datetime import date

class TDReader:
    TABLES = {'daily_kline': ('core_data.daily_kline_v1', 'ts'),
              'daily_dde': ('core_data.daily_dde_v1', 'ts'),
              'chip': ('core_data.chip_indicators_v1', 'ts'),
              'price_volume': ('core_data.price_volume_indicators_v1', 'ts'),
              'ticks': ('market_data1.stock_tick_v2', 'ts'),
              'limit_up_detail': ('core_data.limit_up_detail_v1', 'trade_date')}
    COLUMNS = {'daily_kline': 'ts,symbol,open,high,low,close,pre_close,pct_chg,amount_yuan,turnover_pct',
               'daily_dde': 'ts,symbol,ddje',
               'chip': 'ts,symbol,profit_ratio,available_at',
               'price_volume': 'ts,symbol,ma20,available_at'}
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
                cur.execute(f"SELECT {self.COLUMNS.get(dataset, chr(42))} FROM {table} WHERE {column} >= '{start.isoformat()} 00:00:00' AND {column} < '{(end.fromordinal(end.toordinal()+1)).isoformat()} 00:00:00'")
                names = [item[0] for item in cur.description]
                return [dict(zip(names, row)) for row in cur.fetchall()]
            finally:
                cur.close()
        finally:
            conn.close()

    def read_ticks(self, day, cutoff):
        date.fromisoformat(day)
        from datetime import time
        time.fromisoformat(cutoff)
        import taos
        conn = taos.connect(**self.connect_options)
        try:
            cur = conn.cursor()
            try:
                cur.execute(f"SELECT symbol, LAST_ROW(*) FROM market_data1.stock_tick_v2 WHERE ts >= '{day} 09:15:00' AND ts <= '{day} {cutoff}' PARTITION BY symbol")
                names = [item[0].removeprefix('last_row(').removesuffix(')') for item in cur.description]
                return [dict(zip(names, row)) for row in cur.fetchall()]
            finally:
                cur.close()
        finally:
            conn.close()

    def read_members(self, asof, retrospective):
        import taos
        from engine_core.data_pipeline.reference_snapshot_td import ReferenceSnapshotStore
        with ReferenceSnapshotStore(connect=lambda: taos.connect(**self.connect_options)) as store:
            return store.read_latest('plate_members') if retrospective else store.read_as_of('plate_members', asof)
