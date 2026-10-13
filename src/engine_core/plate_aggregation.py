"""Deterministic, strategy-free auction/opening facts by plate."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Mapping

from .plate_membership import PlateHierarchy, StockPlateMemberships, members_of
from .plate_kind import classify_plate_kind
from .plate_rules import PLATE_RULES_VERSION, is_generic_plate, is_hard_generic_plate


@dataclass(frozen=True)
class StockFact:
    symbol: str
    pre_close: float | None
    price: float | None
    amount: float | None
    limit_state: int | None
    lb_days: int | None
    in_yest_pool: bool = False
    amount_prev: float | None = None


@dataclass(frozen=True)
class MarketSummary:
    covered_count: int
    amount_covered_count: int
    amount_sum: float | None
    pct_equal_weight: float | None


@dataclass(frozen=True)
class PlateStat:
    plate: str
    kind: str
    generic: bool
    hard_generic: bool
    member_count: int
    covered_count: int
    amount_covered_count: int
    amount_sum: float | None
    amount_share_of_market: float | None
    pct_equal_weight: float | None
    pct_amount_weighted: float | None
    excess_vs_market: float | None
    up_count: int
    down_count: int
    flat_count: int
    limit_up_count: int
    limit_down_count: int
    limit_state_unknown_count: int
    max_lb_days: int | None
    yest_pool_member_count: int
    reason_hit_count: int
    tier_counts: Mapping[int, int]
    amount_delta_sum: float | None
    flags: tuple[str, ...]


@dataclass(frozen=True)
class PlateAggregation:
    stats: tuple[PlateStat, ...]
    market: MarketSummary
    rule_version: str
    content_hash: str


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _amount_sum(values: list[float]) -> float | None:
    return sum(values) if values else None


def market_summary(facts: Mapping[str, StockFact]) -> MarketSummary:
    returns = [
        fact.price / fact.pre_close - 1.0
        for fact in facts.values()
        if fact.price is not None and fact.pre_close is not None and fact.pre_close > 0
    ]
    amounts = [fact.amount for fact in facts.values() if fact.amount is not None]
    return MarketSummary(len(returns), len(amounts), _amount_sum(amounts), _mean(returns))


def _payload(stat: PlateStat) -> dict:
    return {
        "plate": stat.plate, "kind": stat.kind,
        "generic": stat.generic, "hard_generic": stat.hard_generic,
        "member_count": stat.member_count, "covered_count": stat.covered_count,
        "amount_covered_count": stat.amount_covered_count,
        "amount_sum": stat.amount_sum, "amount_share_of_market": stat.amount_share_of_market,
        "pct_equal_weight": stat.pct_equal_weight, "pct_amount_weighted": stat.pct_amount_weighted,
        "excess_vs_market": stat.excess_vs_market, "up_count": stat.up_count,
        "down_count": stat.down_count, "flat_count": stat.flat_count,
        "limit_up_count": stat.limit_up_count, "limit_down_count": stat.limit_down_count,
        "limit_state_unknown_count": stat.limit_state_unknown_count,
        "max_lb_days": stat.max_lb_days, "yest_pool_member_count": stat.yest_pool_member_count,
        "reason_hit_count": stat.reason_hit_count,
        "tier_counts": {str(tier): stat.tier_counts[tier] for tier in range(1, 5)},
        "amount_delta_sum": stat.amount_delta_sum, "flags": list(stat.flags),
    }


def aggregate_plates(
    memberships: StockPlateMemberships,
    hierarchy: PlateHierarchy,
    facts: Mapping[str, StockFact],
    *,
    include_ancestors: bool = True,
) -> PlateAggregation:
    direct_plates = {row.plate for rows in memberships.by_symbol.values() for row in rows}
    plates = set(direct_plates)
    if include_ancestors:
        for child, parent in hierarchy.parent_by_child.items():
            if child in direct_plates:
                plates.add(parent)
        # Include all ancestors, including those several levels above a direct plate.
        changed = True
        while changed:
            before = len(plates)
            plates.update(parent for child, parent in hierarchy.parent_by_child.items() if child in plates)
            changed = len(plates) != before

    market = market_summary(facts)
    rows: list[PlateStat] = []
    for plate in plates:
        symbols = members_of(plate, memberships, hierarchy)
        # For ancestor members, preserve the best tier among memberships in the subtree.
        subtree = {plate}
        changed = True
        while changed:
            before = len(subtree)
            subtree.update(child for child, parent in hierarchy.parent_by_child.items() if parent in subtree)
            changed = len(subtree) != before
        tier_by_symbol: dict[str, int] = {}
        for symbol in symbols:
            for membership in memberships.by_symbol.get(symbol, ()):
                if membership.plate in subtree:
                    tier_by_symbol[symbol] = min(tier_by_symbol.get(symbol, 5), membership.tier)

        covered: list[tuple[StockFact, float]] = []
        for symbol in symbols:
            fact = facts.get(symbol)
            if fact is not None and fact.price is not None and fact.pre_close is not None and fact.pre_close > 0:
                covered.append((fact, fact.price / fact.pre_close - 1.0))
        returns = [change for _, change in covered]
        known_facts = [facts[symbol] for symbol in symbols if symbol in facts]
        amount_facts = [fact for fact in known_facts if fact.amount is not None]
        amounts = [fact.amount for fact in amount_facts]
        total_amount = _amount_sum(amounts)
        covered_amounts = [fact.amount for fact, _ in covered if fact.amount is not None]
        weighted_denom = sum(covered_amounts)
        weighted = (
            sum(fact.amount * change for fact, change in covered if fact.amount is not None) / weighted_denom
            if weighted_denom != 0 and covered_amounts else None
        )
        tiers = {tier: sum(1 for value in tier_by_symbol.values() if value == tier) for tier in range(1, 5)}
        deltas = [fact.amount - fact.amount_prev for fact in known_facts
                  if fact.amount is not None and fact.amount_prev is not None]
        rows.append(PlateStat(
            plate=plate, kind=classify_plate_kind(plate),
            generic=is_generic_plate(plate), hard_generic=is_hard_generic_plate(plate),
            member_count=len(symbols), covered_count=len(covered),
            amount_covered_count=len(amount_facts), amount_sum=total_amount,
            amount_share_of_market=(total_amount / market.amount_sum
                                    if total_amount is not None and market.amount_sum not in (None, 0) else None),
            pct_equal_weight=_mean(returns), pct_amount_weighted=weighted,
            excess_vs_market=(_mean(returns) - market.pct_equal_weight
                              if returns and market.pct_equal_weight is not None else None),
            up_count=sum(change > 0 for change in returns), down_count=sum(change < 0 for change in returns),
            flat_count=sum(change == 0 for change in returns),
            limit_up_count=sum(fact.limit_state == 1 for fact in known_facts),
            limit_down_count=sum(fact.limit_state == -1 for fact in known_facts),
            limit_state_unknown_count=sum(
                symbol not in facts or facts[symbol].limit_state is None for symbol in symbols
            ),
            max_lb_days=max((fact.lb_days for fact in known_facts if fact.lb_days is not None), default=None),
            yest_pool_member_count=sum(fact.in_yest_pool for fact in known_facts),
            reason_hit_count=tiers[2], tier_counts=tiers,
            amount_delta_sum=_amount_sum(deltas), flags=(),
        ))
    kind_order = {"THEME": 0, "EVENT": 1, "GENERIC": 2, "NOISE": 3}
    rows.sort(key=lambda row: (kind_order[row.kind], row.amount_sum is None,
                               -(row.amount_sum or 0.0), row.plate))
    payload = {"rule_version": PLATE_RULES_VERSION, "market": market.__dict__,
               "stats": [_payload(row) for row in rows]}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return PlateAggregation(tuple(rows), market, PLATE_RULES_VERSION, digest)


__all__ = ["StockFact", "MarketSummary", "PlateStat", "PlateAggregation", "market_summary", "aggregate_plates"]
