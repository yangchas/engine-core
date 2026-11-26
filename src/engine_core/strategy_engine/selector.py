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
