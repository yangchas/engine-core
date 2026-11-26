"""Read-only loading of research artifacts; no market writes or broker access."""
import gzip
import pickle
from .contracts import MarketSnapshot


def load_dataset(path):
    # Research pickle files must be trusted: unpickling can execute code.
    with gzip.open(path, 'rb') as stream:
        return pickle.load(stream)


def raw_from_point(point):
    factors = point['factors']
    market, ext, ladder = factors['market_global'], factors['extremes'], factors['ladder_relay']
    two = ladder['2p']['avg_pct']
    return {'trade_date': point['trade_date'], 'time': '09:45',
            'up_ratio': market.get('up_ratio'), 'net_lu': ext['sealed_up'] - ext['sealed_down'],
            'top100_avg_pct': factors['top_amount_core'].get('avg_pct'),
            'yest_2p_avg_pct': two if two is not None else ladder['1b']['avg_pct'],
            'high_minus_low_pp': factors['leader_divergence'].get('high_minus_low_pp'),
            'yw_pp': market.get('yw_pp'), 'pos20_med': market.get('pos20_med'),
            'gap': factors['theme_competition'].get('top1_vs_top2_gap_pp')}


def history_records(dataset):
    records = []
    for day in dataset['intraday']:
        point = next((p for p in day['timepoints'] if p['time'] == '09:45'), None)
        if point:
            records.append(raw_from_point(point))
    return records


def snapshots_from_dataset(dataset, reader):
    """Convert an explicitly normalized snapshot dataset.

    Sentiment factor datasets alone do not contain candidate membership, names,
    09:25 quotes or prior landmine facts. Reject them rather than invent them.
    """
    records = dataset.get('strategy_snapshots')
    if records is None:
        raise ValueError('dataset lacks strategy_snapshots: candidate membership, names, 09:25 quotes and prior landmine facts are required')
    for record in records:
        yield MarketSnapshot(**record)
