"""Theme and candidate selection."""
from dataclasses import dataclass
from math import isfinite
from .contracts import SelectionResult


def _get(row, key, default=None):
    return row.get(key, default) if isinstance(row, dict) else getattr(row, key, default)


def _number(value):
    return isinstance(value, (float, int)) and isfinite(value)


@dataclass(frozen=True)
class MainThemeNormalizedAccelSelector:
    top_themes: int = 3
    top_k: int = 5
    max_gem: int = 2
    min_ipos: float = .70
    min_amt_yuan: float = 3e7
    max_ret_limit_ratio: float = .80

    def select(self, snapshot):
        themes = snapshot.themes
        if isinstance(themes, dict):
            themes = [dict(v, theme=k) if isinstance(v, dict) else {'theme': k, 'p45': v} for k, v in themes.items()]
        ranked = sorted((t for t in themes if _number(_get(t, 'p45'))), key=lambda t: (-_get(t, 'p45'), str(_get(t, 'theme', _get(t, 'name', '')))))
        names = tuple(_get(t, 'theme', _get(t, 'name')) for t in ranked[:self.top_themes])
        pool = [c for c in snapshot.candidates if _get(c, 'theme') in names]
        counts = {'theme_pool': len(pool)}
        pool = [c for c in pool if str(_get(c, 'symbol', '')).startswith(('00', '60', '300', '301')) and _get(c, 'name') and 'ST' not in _get(c, 'name').upper() and '退' not in _get(c, 'name')]
        counts['after_universe'] = len(pool)
        pool = [c for c in pool if _number(_get(c, 'prev_streak')) and _get(c, 'prev_streak') < 2 and _get(c, 'prev_broken') == 0]
        counts['after_landmine'] = len(pool)
        pool = [c for c in pool if _number(_get(c, 'ask1')) and _get(c, 'ask1') > 0 and _number(_get(c, 'av1')) and _get(c, 'av1') > 0]
        counts['after_buyable'] = len(pool)
        def limit(c):
            return 20.0 if _get(c, 'symbol').startswith(('300', '301')) else 10.0
        pool = [c for c in pool if all(_number(_get(c, k)) for k in ('intraday_pos_45', 'amt_45_yuan', 'ret_45', 'ret_25')) and _get(c, 'intraday_pos_45') >= self.min_ipos and _get(c, 'amt_45_yuan') >= self.min_amt_yuan and _get(c, 'ret_45') <= self.max_ret_limit_ratio * limit(c)]
        counts['after_guardrails'] = len(pool)
        pool.sort(key=lambda c: (-(_get(c, 'ret_45') - _get(c, 'ret_25')) / limit(c), _get(c, 'symbol')))
        picks, evidence, seen, gems = [], [], set(), 0
        for c in pool:
            symbol = _get(c, 'symbol')
            gem = symbol.startswith(('300', '301'))
            if symbol in seen or (gem and gems >= self.max_gem):
                continue
            if len(picks) >= self.top_k:
                break
            picks.append((symbol, (_get(c, 'ret_45') - _get(c, 'ret_25')) / limit(c), _get(c, 'ask1'), _get(c, 'theme')))
            path = _get(c, 'ret_45') - _get(c, 'ret_25')
            evidence.append({**{k: _get(c, k) for k in ('symbol', 'name', 'theme', 'ret_25', 'ret_45', 'intraday_pos_45', 'ask1', 'av1', 'prev_streak', 'prev_broken')},
                             'rank': len(picks), 'board': '创业板' if gem else '主板', 'limit_pct': limit(c),
                             'path_25_45': path, 'norm_accel': path / limit(c), 'amt_45_yi': _get(c, 'amt_45_yuan') / 1e8})
            seen.add(symbol)
            gems += int(gem)
        counts.update(selected=len(picks), gem_count=gems)
        details = tuple({'theme': _get(t, 'theme', _get(t, 'name')), 'p45': _get(t, 'p45')} for t in ranked[:self.top_themes])
        return SelectionResult(names, tuple(picks), counts, details, tuple(evidence))


@dataclass(frozen=True)
class LowPos20SectorSelector:
    top_themes: int = 3
    top_k: int = 5
    max_gem: int = 2
    min_theme_members: int = 5
    prefer_up45_in_bear: bool = False

    def select(self, snapshot):
        themes = snapshot.themes
        if isinstance(themes, dict):
            themes = [dict(v, theme=k) for k, v in themes.items()]
        themes = [t for t in themes if _number(_get(t, 'pos20_prev')) and
                  (_get(t, 'members', _get(t, 'n')) is None or
                   (isinstance(_get(t, 'members', _get(t, 'n')), (int, float)) and _get(t, 'members', _get(t, 'n')) >= self.min_theme_members))]
        def percentile(value, values):
            return (sum(x < value for x in values) + .5 * sum(x == value for x in values)) / len(values)
        if self.prefer_up45_in_bear and snapshot.regime_raw.get('macro_state') == 'BEAR':
            # Missing breadth is ranked below every observed breadth.
            pos = [-_get(t, 'pos20_prev') for t in themes]
            breadth = [_get(t, 'up45') if _number(_get(t, 'up45')) else -1 for t in themes]
            themes = sorted(themes, key=lambda t: (-(percentile(-_get(t, 'pos20_prev'), pos) + percentile(_get(t, 'up45') if _number(_get(t, 'up45')) else -1, breadth)), str(_get(t, 'theme', _get(t, 'name', '')))))
        else:
            themes = sorted(themes, key=lambda t: (_get(t, 'pos20_prev'), str(_get(t, 'theme', _get(t, 'name', '')))))
        themes = themes[:self.top_themes]
        names = tuple(_get(t, 'theme', _get(t, 'name')) for t in themes)
        pool = [c for c in snapshot.candidates if _get(c, 'theme') in names]
        counts = {'theme_pool': len(pool)}
        pool = [c for c in pool if str(_get(c, 'symbol', '')).startswith(('00', '60', '300', '301')) and _get(c, 'name') and 'ST' not in _get(c, 'name').upper() and '退' not in _get(c, 'name') and _number(_get(c, 'pos20_prev'))]
        counts['after_universe'] = len(pool)
        pool = [c for c in pool if _number(_get(c, 'ask1')) and _get(c, 'ask1') > 0 and _number(_get(c, 'av1')) and _get(c, 'av1') > 0]
        counts['after_buyable'] = len(pool)
        def accel(c):
            value = _get(c, 'norm_accel')
            if _number(value): return value
            a, b = _get(c, 'ret_45'), _get(c, 'ret_25')
            return (a-b)/(20 if _get(c, 'symbol').startswith(('300', '301')) else 10) if _number(a) and _number(b) else None
        pool.sort(key=lambda c: (_get(c, 'pos20_prev'), -(accel(c) if accel(c) is not None else float('-inf')), _get(c, 'symbol')))
        picks, evidence, seen, gems = [], [], set(), 0
        for c in pool:
            symbol = _get(c, 'symbol'); gem = symbol.startswith(('300', '301'))
            if symbol in seen or (gem and gems >= self.max_gem): continue
            if len(picks) >= self.top_k: break
            a, b, amount = _get(c, 'ret_45'), _get(c, 'ret_25'), _get(c, 'amt_45_yuan')
            score = accel(c)
            picks.append((symbol, score, _get(c, 'ask1'), _get(c, 'theme')))
            evidence.append({**{k: _get(c, k) for k in ('symbol', 'name', 'theme', 'ret_25', 'ret_45', 'intraday_pos_45', 'ask1', 'av1', 'prev_streak', 'prev_broken', 'pos20_prev')},
                             'rank': len(picks), 'board': '创业板' if gem else '主板', 'limit_pct': 20 if gem else 10,
                             'path_25_45': a-b if _number(a) and _number(b) else None, 'norm_accel': score,
                             'amt_45_yi': amount / 1e8 if _number(amount) else None})
            seen.add(symbol); gems += int(gem)
        counts.update(selected=len(picks), gem_count=gems)
        return SelectionResult(names, tuple(picks), counts, tuple(dict(t) for t in themes), tuple(evidence))
