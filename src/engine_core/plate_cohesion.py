"""Strategy-free, time-aligned plate and member behavior facts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from statistics import median
from typing import Mapping, Sequence

from .plate_kind import classify_plate_kind
from .plate_membership import PlateHierarchy, StockPlateMemberships, members_of


@dataclass(frozen=True)
class PlateCohesion:
    plate: str
    member_count: int
    covered_count: int
    same_direction_share: Mapping[str, float] | None
    pct_dispersion_iqr: float | None
    pct_median: float | None
    amount_concentration_top3: float | None
    amount_delta_up_share: float | None
    amount_delta_down_share: float | None
    speed_median: float | None
    speed_same_direction_share: float | None
    flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class StockAffinity:
    symbol: str
    plate: str
    corr0: float | None
    best_lag_points: int | None
    best_lag_ms: int | None
    best_corr: float | None
    role: str
    contribution_share: float | None


@dataclass(frozen=True)
class ActivePlateEvidence:
    symbol: str
    plate: str
    status: str
    evidence: StockAffinity


@dataclass(frozen=True)
class PlateCohesionResult:
    cohesion: tuple[PlateCohesion, ...]
    affinities: tuple[StockAffinity, ...]
    grid_ms: int
    grid_points: int


def align_asof_grid(
    series: Mapping[str, Sequence[tuple]],
    grid_start_ms: int,
    grid_end_ms: int,
    *,
    grid_ms: int = 3000,
) -> dict[str, list[tuple]]:
    """Sample each input at fixed grid points using only observations at or before them."""
    if grid_ms <= 0 or grid_end_ms < grid_start_ms:
        raise ValueError("invalid grid bounds")
    grid = list(range(grid_start_ms, grid_end_ms + 1, grid_ms))
    result: dict[str, list[tuple]] = {}
    for symbol, raw_rows in series.items():
        rows = sorted(raw_rows, key=lambda row: row[0])
        cursor = 0
        latest: tuple | None = None
        latest_pc = None
        sampled = []
        for point in grid:
            while cursor < len(rows) and rows[cursor][0] <= point:
                latest = rows[cursor]
                if len(latest) >= 4 and latest[3] is not None:
                    latest_pc = latest[3]
                cursor += 1
            if latest is None:
                sampled.append((point, None, None, None))
            elif len(latest) >= 4:
                sampled.append((point, latest[1], latest[2], latest_pc))
            else:
                sampled.append((point, latest[1], latest[2]))
        result[str(symbol)] = sampled
    return result


def _pearson(left: Sequence[float | None], right: Sequence[float | None], min_points: int) -> float | None:
    pairs = [(a, b) for a, b in zip(left, right) if a is not None and b is not None]
    if len(pairs) < min_points:
        return None
    xs, ys = [p[0] for p in pairs], [p[1] for p in pairs]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    dx, dy = [x - mx for x in xs], [y - my for y in ys]
    vx, vy = sum(x * x for x in dx), sum(y * y for y in dy)
    if vx <= 0 or vy <= 0:
        return None
    return sum(x * y for x, y in zip(dx, dy)) / math.sqrt(vx * vy)


def _diff(values: Sequence[float | None]) -> list[float | None]:
    return [None if a is None or b is None else b - a for a, b in zip(values, values[1:])]


def lead_lag(
    plate_median_series: Sequence[float | None],
    stock_series: Sequence[float | None],
    *,
    max_lag_points: int = 10,
    min_points: int = 10,
) -> tuple[int | None, float | None]:
    """Correlate first differences; positive lag means the stock follows the plate."""
    if max_lag_points < 0:
        raise ValueError("max_lag_points must be nonnegative")
    plate_delta, stock_delta = _diff(plate_median_series), _diff(stock_series)
    best_lag: int | None = None
    best_corr: float | None = None
    for lag in range(-max_lag_points, max_lag_points + 1):
        if lag > 0:
            left, right = plate_delta[:-lag], stock_delta[lag:]
        elif lag < 0:
            left, right = plate_delta[-lag:], stock_delta[:lag]
        else:
            left, right = plate_delta, stock_delta
        corr = _pearson(left, right, min_points)
        if corr is not None and (best_corr is None or corr > best_corr):
            best_lag, best_corr = lag, corr
    return best_lag, best_corr


def _direction(values: Sequence[float]) -> Mapping[str, float]:
    if not values:
        return {"up": 0.0, "down": 0.0, "flat": 0.0}
    total = len(values)
    up = sum(value > 0 for value in values)
    down = sum(value < 0 for value in values)
    return {"up": up / total, "down": down / total, "flat": (total - up - down) / total}


def _quartile(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def calculate_plate_cohesion(
    series: Mapping[str, Sequence[tuple]],
    pre_close: Mapping[str, float | None],
    memberships: StockPlateMemberships,
    hierarchy: PlateHierarchy,
    *,
    grid_start_ms: int,
    grid_end_ms: int,
    grid_ms: int = 3000,
    window_ms: int = 30000,
    min_members: int = 5,
    min_points: int = 10,
    max_lag_points: int = 10,
) -> PlateCohesionResult:
    if window_ms <= 0 or min_members < 1 or min_points < 1:
        raise ValueError("window_ms, min_members, and min_points must be positive")
    aligned = align_asof_grid(series, grid_start_ms, grid_end_ms, grid_ms=grid_ms)
    points = list(range(grid_start_ms, grid_end_ms + 1, grid_ms))
    eligible = {s: tuple(r for r in rows if classify_plate_kind(r.plate) in ("THEME", "GENERIC"))
                for s, rows in memberships.by_symbol.items()}
    names = sorted({r.plate for rows in eligible.values() for r in rows})
    # Include eligible ancestors so hierarchy rollups use the union of descendant members.
    names = sorted(set(names) | {p for p in hierarchy.parent_by_child.values()
                                 if classify_plate_kind(p) in ("THEME", "GENERIC")})
    filtered = StockPlateMemberships(eligible, memberships.policy, memberships.rule_version,
                                    memberships.content_hash, memberships.legacy_weights)
    output: list[PlateCohesion] = []
    affinities: list[StockAffinity] = []
    for plate in names:
        if classify_plate_kind(plate) not in ("THEME", "GENERIC"):
            continue
        symbols = sorted(members_of(plate, filtered, hierarchy))
        pct: dict[str, list[float | None]] = {}
        amounts: dict[str, list[float | None]] = {}
        closes_by_symbol: dict[str, list[float]] = {}
        for symbol in symbols:
            rows = aligned.get(symbol, [(p, None, None) for p in points])
            pc = pre_close.get(symbol)
            pct[symbol] = []
            closes_by_symbol[symbol] = []
            for row in rows:
                _, price, amount = row[:3]
                point_pc = row[3] if len(row) >= 4 and row[3] is not None else pc
                valid_pc = point_pc is not None and math.isfinite(float(point_pc)) and float(point_pc) > 0
                if valid_pc:
                    closes_by_symbol[symbol].append(float(point_pc))
                pct[symbol].append(
                    (float(price) / float(point_pc) - 1.0) * 100.0
                    if price is not None and valid_pc and math.isfinite(float(price)) else None
                )
            amounts[symbol] = [float(row[2]) if row[2] is not None and math.isfinite(float(row[2])) else None
                               for row in rows]
        covered = sum(any(value is not None for value in values) for values in pct.values())
        eligible_grid_count = sum(any(pct[s][i] is not None for s in symbols) for i in range(len(points)))
        flags: list[str] = []
        if len(symbols) < min_members:
            flags.append("INSUFFICIENT_MEMBERS")
        if any(len(set(closes_by_symbol[symbol])) > 1 for symbol in symbols):
            flags.append("pc_changed_in_window")
        if eligible_grid_count < min_points:
            flags.append("INSUFFICIENT_POINTS")
        insufficient = "INSUFFICIENT_MEMBERS" in flags or "INSUFFICIENT_POINTS" in flags
        latest_pct = [values[-1] for values in pct.values() if values and values[-1] is not None]
        latest_amount = [values[-1] for values in amounts.values() if values and values[-1] is not None]
        if insufficient:
            direction = iqr = pct_median = concentration = amount_up = amount_down = speed_median = speed_direction = None
        else:
            direction = _direction(latest_pct)
            iqr = _quartile(latest_pct, .75) - _quartile(latest_pct, .25) if latest_pct else None
            pct_median = median(latest_pct) if latest_pct else None
            total_amount = sum(latest_amount)
            concentration = (sum(sorted(latest_amount, reverse=True)[:3]) / total_amount
                             if latest_amount and total_amount > 0 else None)
            up_shares = []
            down_shares = []
            for i in range(1, len(points)):
                changes = [amounts[s][i] - amounts[s][i - 1] for s in symbols
                           if amounts[s][i] is not None and amounts[s][i - 1] is not None]
                if changes:
                    up_shares.append(sum(change > 0 for change in changes) / len(changes))
                    down_shares.append(sum(change < 0 for change in changes) / len(changes))
            amount_up = median(up_shares) if up_shares else None
            amount_down = median(down_shares) if down_shares else None
            cutoff_index = max(0, next((i for i, p in enumerate(points) if p >= points[-1] - window_ms), len(points)-1))
            speeds = [values[-1] - values[cutoff_index] for values in pct.values()
                      if len(values) > cutoff_index and values[-1] is not None and values[cutoff_index] is not None]
            speed_median = median(speeds) if speeds else None
            speed_direction = max(_direction(speeds).values()) if speeds else None
        output.append(PlateCohesion(plate, len(symbols), covered, direction, iqr, pct_median,
                                    concentration, amount_up, amount_down, speed_median, speed_direction,
                                    tuple(flags)))
        plate_series: list[float | None] = []
        for i in range(len(points)):
            vals = [values[i] for values in pct.values()
                    if values[i] is not None]
            plate_series.append(median(vals) if vals else None)
        for symbol in symbols:
            stock = pct[symbol]
            leave_one_out = []
            for i in range(len(points)):
                vals = [pct[other][i] for other in symbols if other != symbol and pct[other][i] is not None]
                leave_one_out.append(median(vals) if vals else None)
            lag, corr = lead_lag(leave_one_out, stock, max_lag_points=max_lag_points, min_points=min_points)
            corr0 = _pearson(_diff(leave_one_out), _diff(stock), min_points)
            if "INSUFFICIENT_MEMBERS" in flags or lag is None or corr is None:
                role = "UNKNOWN"
            elif abs(lag) <= 1:
                role = "SYNC"
            elif lag > 1:
                role = "FOLLOWER"
            else:
                role = "LEADER"
            amount = amounts[symbol][-1] if amounts[symbol] else None
            total = sum(value for value in latest_amount)
            contribution = amount / total if amount is not None and total > 0 else None
            affinities.append(StockAffinity(symbol, plate, corr0, lag,
                                            lag * grid_ms if lag is not None else None,
                                            corr, role, contribution))
    return PlateCohesionResult(tuple(output), tuple(affinities), grid_ms, len(points))


def active_plates_for_stock(
    affinities: Sequence[StockAffinity], *, min_corr: float = 0.3,
) -> list[ActivePlateEvidence]:
    result = []
    for row in affinities:
        supported = row.best_corr is not None and row.best_corr >= min_corr and row.role != "UNKNOWN"
        result.append(ActivePlateEvidence(row.symbol, row.plate, "ACTIVE" if supported else "UNKNOWN", row))
    return result


def to_dict(value: object) -> object:
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    raise TypeError(f"unsupported value: {type(value)!r}")


__all__ = ["ActivePlateEvidence", "PlateCohesion", "PlateCohesionResult", "StockAffinity",
           "active_plates_for_stock", "align_asof_grid", "calculate_plate_cohesion", "lead_lag", "to_dict"]
