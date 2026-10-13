"""Phase-isolated plate cohesion calculations with the auction/opening gap removed."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
from statistics import median
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo
from datetime import datetime

from .plate_cohesion import _direction, _quartile, _pearson, lead_lag
from .plate_kind import classify_plate_kind
from .plate_membership import PlateHierarchy, StockPlateMemberships, members_of

_ZONE = ZoneInfo("Asia/Shanghai")
_PHASES = ("AUCTION", "CALL_RESULT", "OPEN")


@dataclass(frozen=True)
class PhasePlateCohesion:
    phase: str
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
    amount_progress: Mapping[str, float | None] | None
    flags: tuple[str, ...] = ()
    matched_amount_sum: tuple[tuple[int, float], ...] = ()
    rest_bid_amount_sum: float | None = None
    rest_ask_amount_sum: float | None = None
    net_unmatched_direction: Mapping[str, float] | None = None
    sub_phase_amounts: Mapping[str, float | None] | None = None


@dataclass(frozen=True)
class PhaseStockAffinity:
    phase: str
    symbol: str
    plate: str
    corr0: float | None
    best_lag_points: int | None
    best_lag_ms: int | None
    best_corr: float | None
    role: str


@dataclass(frozen=True)
class PhaseCohesionResult:
    by_phase: Mapping[str, tuple[PhasePlateCohesion, ...]]
    affinities_by_phase: Mapping[str, tuple[PhaseStockAffinity, ...]]
    flags: Mapping[str, object]
    content_hash: str


def _clock_ms(reference_ms: int, clock: str) -> int:
    local = datetime.fromtimestamp(reference_ms / 1000, _ZONE)
    hour, minute, second = map(int, clock.split(":"))
    return int(local.replace(hour=hour, minute=minute, second=second, microsecond=0).timestamp() * 1000)


def _sample(rows: Sequence[tuple], points: Sequence[int]) -> list[tuple[int, float | None, float | None]]:
    ordered = sorted(rows, key=lambda row: row[0])
    cursor = 0
    latest = None
    result = []
    for point in points:
        while cursor < len(ordered) and ordered[cursor][0] <= point:
            latest = ordered[cursor]
            cursor += 1
        result.append((point, latest[1] if latest else None, latest[2] if latest else None))
    return result


def compute_phase_cohesion(
    series_by_symbol: Mapping[str, Sequence[tuple]],
    pre_close: Mapping[str, float | None],
    memberships: StockPlateMemberships,
    hierarchy: PlateHierarchy,
    *,
    amount_snapshots: Mapping[str, Mapping[str, float | None]] | None = None,
    price_snapshots: Mapping[str, Mapping[str, float | None]] | None = None,
    phases: Sequence[str] = _PHASES,
    grid_ms: int = 3000,
    open_end: str = "09:45:00",
    window_ms: int = 30000,
    min_members: int = 5,
    min_points: int = 10,
    max_lag_points: int = 10,
) -> PhaseCohesionResult:
    if grid_ms <= 0 or window_ms <= 0 or min_members < 1 or min_points < 1:
        raise ValueError("grid/window/minimum values must be positive")
    selected = tuple(phase.upper() for phase in phases)
    if any(phase not in _PHASES for phase in selected):
        raise ValueError("unsupported phase")
    refs = [row[0] for rows in series_by_symbol.values() for row in rows]
    if not refs:
        refs = [int(datetime.now(_ZONE).timestamp() * 1000)]
    reference = min(refs)
    start_auction, end_auction = _clock_ms(reference, "09:15:00"), _clock_ms(reference, "09:25:00")
    call_result = end_auction
    end_call_result = end_auction + 60_000
    open_start, end_open = _clock_ms(reference, "09:30:00"), _clock_ms(reference, open_end)
    gap_points = len(range(end_auction, open_start, grid_ms))
    eligible = {s: tuple(r for r in rows if classify_plate_kind(r.plate) in ("THEME", "GENERIC"))
                for s, rows in memberships.by_symbol.items()}
    names = sorted({r.plate for rows in eligible.values() for r in rows})
    names = sorted(set(names) | {p for p in hierarchy.parent_by_child.values()
                                 if classify_plate_kind(p) in ("THEME", "GENERIC")})
    filtered = StockPlateMemberships(eligible, memberships.policy, memberships.rule_version,
                                     memberships.content_hash, memberships.legacy_weights)
    snapshots = amount_snapshots or {}
    prices = price_snapshots or {}
    by_phase = {}
    affinities_by_phase = {}
    phase_sample_counts = {}
    for phase in selected:
        if phase == "AUCTION":
            points = list(range(start_auction, end_auction, grid_ms))
        elif phase == "CALL_RESULT":
            points = [call_result]
        else:
            points = list(range(open_start, end_open, grid_ms))
        phase_sample_counts[phase] = len(points)
        plate_rows = []
        affinities = []
        for plate in names:
            if classify_plate_kind(plate) not in ("THEME", "GENERIC"):
                continue
            symbols = sorted(members_of(plate, filtered, hierarchy))
            phase_rows = {
                s: [(r[0], *r[1:]) for r in series_by_symbol.get(s, ())
                    if ((phase == "OPEN" and r[0] >= open_start)
                        or (phase == "AUCTION" and start_auction <= r[0] < end_auction)
                        or (phase == "CALL_RESULT" and end_auction <= r[0] < end_call_result))]
                for s in symbols
            }
            sampled = {s: _sample(phase_rows[s], points) for s in symbols}
            if phase == "CALL_RESULT":
                snapshot_prices = prices.get("0925", {})
                for symbol in symbols:
                    if snapshot_prices.get(symbol) is not None:
                        sampled[symbol] = [(call_result, float(snapshot_prices[symbol]),
                                            snapshots.get("0925", {}).get(symbol))]
            pct = {}
            raw_amount = {}
            for symbol in symbols:
                pc = pre_close.get(symbol)
                pct[symbol] = [((float(row[1]) / float(pc) - 1) * 100
                                if row[1] is not None and pc is not None and float(pc) > 0 else None)
                               for row in sampled[symbol]]
                raw_amount[symbol] = [row[2] for row in sampled[symbol]]
            covered = sum(any(v is not None for v in vals) for vals in pct.values())
            active_points = sum(any(vals[i] is not None for vals in pct.values()) for i in range(len(points)))
            row_flags = []
            if len(symbols) < min_members:
                row_flags.append("INSUFFICIENT_MEMBERS")
            if active_points < min_points:
                row_flags.append("INSUFFICIENT_POINTS")
            final_pct = [v[-1] for v in pct.values() if v and v[-1] is not None]
            direction = _direction(final_pct) if final_pct else None
            dispersion = _quartile(final_pct, .75) - _quartile(final_pct, .25) if final_pct else None
            pct_mid = median(final_pct) if final_pct else None
            amount_progress = None
            changes: list[float] = []
            interval_up: list[float] = []
            interval_down: list[float] = []
            latest_amounts: list[float] = []
            matched_amount_sum: tuple[tuple[int, float], ...] = ()
            rest_bid_amount_sum = rest_ask_amount_sum = 0.0
            book_rows = 0
            net_unmatched_direction = None
            sub_phase_amounts = None
            if phase == "AUCTION":
                event_amounts: dict[int, float] = {}
                sub_phase_amounts = {"CANCELLABLE": 0.0, "LOCKED": 0.0}
                sub_phase_counts = {"CANCELLABLE": 0, "LOCKED": 0}
                bid_more = ask_more = balanced = 0
                directional = 0
                for symbol in symbols:
                    for raw in phase_rows[symbol]:
                        ts = raw[0]
                        amount = (float(raw[2]) if raw[2] is not None and math.isfinite(float(raw[2]))
                                  else 0.0)
                        event_amounts[ts] = event_amounts.get(ts, 0.0) + amount
                        if ts < _clock_ms(reference, "09:20:00"):
                            sub_phase_amounts["CANCELLABLE"] += amount
                            sub_phase_counts["CANCELLABLE"] += 1
                        else:
                            sub_phase_amounts["LOCKED"] += amount
                            sub_phase_counts["LOCKED"] += 1
                        if len(raw) > 6 and raw[6] in ("BID", "ASK", "BALANCED"):
                            directional += 1
                            bid_more += raw[6] == "BID"
                            ask_more += raw[6] == "ASK"
                            balanced += raw[6] == "BALANCED"
                        if len(raw) > 4:
                            rest_bid_amount_sum += float(raw[4] or 0)
                        if len(raw) > 5:
                            rest_ask_amount_sum += float(raw[5] or 0)
                        if len(raw) > 5:
                            book_rows += 1
                sub_phase_amounts = {key: value if sub_phase_counts[key] else None
                                     for key, value in sub_phase_amounts.items()}
                rest_bid_amount_sum = rest_bid_amount_sum if book_rows else None
                rest_ask_amount_sum = rest_ask_amount_sum if book_rows else None
                matched_amount_sum = tuple(sorted(event_amounts.items()))
                amount_progress = {"matched_amount_sum": sum(event_amounts.values()) if event_amounts else None}
                if len(points) >= 2:
                    for symbol in symbols:
                        values = [float(raw[2]) for raw in phase_rows[symbol]
                                  if raw[2] is not None and math.isfinite(float(raw[2]))]
                        changes.extend(b - a for a, b in zip(values, values[1:]) if a != b)
                if changes:
                    interval_up.append(sum(v > 0 for v in changes) / len(changes))
                    interval_down.append(sum(v < 0 for v in changes) / len(changes))
                latest_amounts = [amount for _, amount in matched_amount_sum]
                net_unmatched_direction = ({"BID": bid_more / directional, "ASK": ask_more / directional,
                                            "BALANCED": balanced / directional}
                                           if directional else None)
            elif phase == "CALL_RESULT":
                snap = snapshots.get("0925", {})
                latest_amounts = [float(snap[s]) for s in set(symbols)
                                  if snap.get(s) is not None and math.isfinite(float(snap[s]))]
                amount_progress = {"0925": sum(latest_amounts)}
                row_flags.append("INSUFFICIENT_POINTS")
            else:
                for symbol in symbols:
                    valid = [float(a) for a in raw_amount[symbol] if a is not None and math.isfinite(float(a))]
                    if valid:
                        latest_amounts.append(valid[-1])
                    baseline = next((float(a) for a in raw_amount[symbol]
                                     if a is not None and math.isfinite(float(a))), None)
                    prev = baseline
                    started = baseline is not None
                    for amount in raw_amount[symbol]:
                        current = float(amount) if amount is not None and math.isfinite(float(amount)) else None
                        if current is None:
                            continue
                        if started and prev is not None and current != prev:
                            changes.append(current - prev)
                        prev = current
                        started = True
            concentration = (sum(sorted(latest_amounts, reverse=True)[:3]) / sum(latest_amounts)
                             if latest_amounts and sum(latest_amounts) > 0 else None)
            amount_up = median(interval_up) if interval_up else (
                sum(x > 0 for x in changes) / len(changes) if changes else None)
            amount_down = median(interval_down) if interval_down else (
                sum(x < 0 for x in changes) / len(changes) if changes else None)
            if phase == "CALL_RESULT" or active_points < min_points or len(symbols) < min_members:
                speed = speed_direction = None
            else:
                cutoff_index = max(0, next((i for i, point in enumerate(points)
                                            if point >= points[-1] - window_ms), len(points) - 1))
                speeds = [values[-1] - values[cutoff_index] for values in pct.values()
                          if len(values) > cutoff_index and values[cutoff_index] is not None
                          and values[-1] is not None]
                speed = median(speeds) if speeds else None
                speed_direction = max(_direction(speeds).values()) if speeds else None
            plate_rows.append(PhasePlateCohesion(
                phase, plate, len(symbols), covered, direction, dispersion, pct_mid, concentration,
                amount_up, amount_down, speed, speed_direction, amount_progress, tuple(dict.fromkeys(row_flags)),
                matched_amount_sum, rest_bid_amount_sum, rest_ask_amount_sum, net_unmatched_direction,
                sub_phase_amounts))
            if phase == "CALL_RESULT":
                continue
            for symbol in symbols:
                stock = pct[symbol]
                leave_one_out = [median(vals) if vals else None for vals in (
                    [pct[other][i] for other in symbols if other != symbol and pct[other][i] is not None]
                    for i in range(len(points)))]
                lag, corr = lead_lag(leave_one_out, stock, max_lag_points=max_lag_points,
                                     min_points=min_points)
                plate_delta = [None if a is None or b is None else b - a
                               for a, b in zip(leave_one_out, leave_one_out[1:])]
                stock_delta = [None if a is None or b is None else b - a for a, b in zip(stock, stock[1:])]
                corr0 = _pearson(plate_delta, stock_delta, min_points)
                role = "UNKNOWN" if lag is None or corr is None or len(symbols) < min_members else (
                    "SYNC" if abs(lag) <= 1 else "FOLLOWER" if lag > 1 else "LEADER")
                affinities.append(PhaseStockAffinity(phase, symbol, plate, corr0, lag,
                                                     lag * grid_ms if lag is not None else None, corr, role))
        by_phase[phase] = tuple(plate_rows)
        affinities_by_phase[phase] = tuple(affinities)
    snapshot_crosscheck = {}
    for tag, clock in (("0920", "09:20:03"), ("0924", "09:24:10"), ("0925", "09:25:06")):
        target = _clock_ms(reference, clock)
        compared = equal = 0
        for symbol, rows in series_by_symbol.items():
            candidates = [r for r in rows if target - 3000 <= r[0] <= target]
            if not candidates:
                continue
            row = max(candidates, key=lambda r: r[0])
            snap_amt = (snapshots.get(tag, {}).get(symbol))
            snap_px = prices.get(tag, {}).get(symbol)
            if snap_amt is None or snap_px is None or row[1] is None or row[2] is None:
                continue
            compared += 1
            if math.isclose(float(row[1]), float(snap_px), abs_tol=1e-9) and math.isclose(
                    float(row[2]), float(snap_amt), abs_tol=1e-9):
                equal += 1
        snapshot_crosscheck[tag] = {"compared": compared, "equal": equal}
    flags = {"gap": "GAP", "gap_skipped_points": gap_points, "phase_sample_counts": phase_sample_counts,
             "snapshot_crosscheck": snapshot_crosscheck,
             "auction_sub_phases": {"CANCELLABLE": ["09:15:00", "09:20:00"],
                                    "LOCKED": ["09:20:00", "09:25:00"]}}
    payload = {"by_phase": {p: [asdict(row) for row in rows] for p, rows in by_phase.items()},
               "affinities_by_phase": {p: [asdict(row) for row in rows] for p, rows in affinities_by_phase.items()},
               "flags": flags}
    content_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                                   allow_nan=False).encode()).hexdigest()
    return PhaseCohesionResult(by_phase, affinities_by_phase, flags, content_hash)


__all__ = ["PhaseCohesionResult", "PhasePlateCohesion", "PhaseStockAffinity", "compute_phase_cohesion"]
