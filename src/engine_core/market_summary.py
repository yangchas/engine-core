"""Pure normalization of the explicit A2 09:25 market summary."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import re
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from .contracts import (
    EVIDENCE_HASH_CONTRACT_VERSION,
    SEMANTIC_HASH_CONTRACT_VERSION,
    deep_freeze,
    evidence_hash,
    semantic_hash,
    trunc_div,
)
from .facts import FactStatus
from .q2 import Q2Quote, normalize_symbol


AUCTION_MARKET_SUMMARY_CONTRACT_VERSION = "AuctionMarketSummaryFactV1"
_STRICT_TRADE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SHANGHAI = ZoneInfo("Asia/Shanghai")

_FIELD_SPECS = (
    ("stock_count", "total_stocks"),
    ("valid_stock_count", "valid_stock_count"),
    ("unavailable_stock_count", "unavailable_stock_count"),
    ("positive_count", "high_open_count"),
    ("negative_count", "low_open_count"),
    ("flat_count", "flat_open_count"),
    ("auction_amount_yuan", "total_auction_amount_yuan"),
    ("limit_up_count", "limit_up_count"),
    ("limit_down_count", "limit_down_count"),
    ("limit_up_seal_amount_yuan", "total_limit_up_bid_amount_yuan"),
)

Q2_AUCTION_SUMMARY_CONTRACT_VERSION = "Q2AuctionSummaryProjectionV1"
Q2_AUCTION_CANDIDATE_RULE = "ANY_POSITIVE(am,br,ar)"
_Q2_SUMMARY_METRICS = (
    "stock_count",
    "valid_stock_count",
    "unavailable_stock_count",
    "positive_count",
    "negative_count",
    "flat_count",
    "auction_amount_yuan",
    "limit_up_count",
    "limit_down_count",
    "limit_up_seal_amount_yuan",
)


@dataclass(frozen=True)
class AuctionMarketSummaryFact:
    """Canonical A2 summary; no plate or strategy interpretation."""

    trade_date: str
    status: FactStatus
    source_id: str
    source: str
    source_table: str
    observation_time_ms: Optional[int]
    stock_count: Optional[int]
    valid_stock_count: Optional[int]
    unavailable_stock_count: Optional[int]
    positive_count: Optional[int]
    negative_count: Optional[int]
    flat_count: Optional[int]
    auction_amount_yuan: Optional[int]
    limit_up_count: Optional[int]
    limit_down_count: Optional[int]
    limit_up_seal_amount_yuan: Optional[int]
    missing_fields: Tuple[str, ...] = ()
    invalid_fields: Tuple[str, ...] = ()
    content_hash: str = field(init=False)
    evidence_hash: str = field(init=False)
    evidence_refs: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "missing_fields", tuple(self.missing_fields))
        object.__setattr__(self, "invalid_fields", tuple(self.invalid_fields))
        object.__setattr__(self, "evidence_refs", tuple(sorted(set(self.evidence_refs))))
        semantic_payload = {
            "contract_version": AUCTION_MARKET_SUMMARY_CONTRACT_VERSION,
            "trade_date": self.trade_date,
            "status": self.status,
            "stock_count": self.stock_count,
            "valid_stock_count": self.valid_stock_count,
            "unavailable_stock_count": self.unavailable_stock_count,
            "positive_count": self.positive_count,
            "negative_count": self.negative_count,
            "flat_count": self.flat_count,
            "auction_amount_yuan": self.auction_amount_yuan,
            "limit_up_count": self.limit_up_count,
            "limit_down_count": self.limit_down_count,
            "limit_up_seal_amount_yuan": self.limit_up_seal_amount_yuan,
            "missing_fields": self.missing_fields,
            "invalid_fields": self.invalid_fields,
        }
        evidence_payload = {
            "contract_version": AUCTION_MARKET_SUMMARY_CONTRACT_VERSION,
            "trade_date": self.trade_date,
            "source_id": self.source_id,
            "source": self.source,
            "source_table": self.source_table,
            "observation_time_ms": self.observation_time_ms,
            "evidence_refs": self.evidence_refs,
            "field_lineage": {
                field_name: (self.source_table,)
                for field_name, _ in _FIELD_SPECS
            },
            "hash_contract_versions": {
                "semantic": SEMANTIC_HASH_CONTRACT_VERSION,
                "evidence": EVIDENCE_HASH_CONTRACT_VERSION,
            },
        }
        object.__setattr__(self, "content_hash", semantic_hash(semantic_payload))
        object.__setattr__(self, "evidence_hash", evidence_hash(evidence_payload))

    def as_mapping(self) -> Mapping[str, Any]:
        """Return canonical names without source-specific raw aliases."""

        return {
            "trade_date": self.trade_date,
            "status": self.status,
            "source_id": self.source_id,
            "source": self.source,
            "source_table": self.source_table,
            "observation_time_ms": self.observation_time_ms,
            "stock_count": self.stock_count,
            "valid_stock_count": self.valid_stock_count,
            "unavailable_stock_count": self.unavailable_stock_count,
            "positive_count": self.positive_count,
            "negative_count": self.negative_count,
            "flat_count": self.flat_count,
            "auction_amount_yuan": self.auction_amount_yuan,
            "limit_up_count": self.limit_up_count,
            "limit_down_count": self.limit_down_count,
            "limit_up_seal_amount_yuan": self.limit_up_seal_amount_yuan,
            "missing_fields": self.missing_fields,
            "invalid_fields": self.invalid_fields,
            "content_hash": self.content_hash,
            "evidence_hash": self.evidence_hash,
            "evidence_refs": self.evidence_refs,
        }


@dataclass(frozen=True)
class Q2AuctionSummaryProjection:
    """Fact-only A2-style aggregate computed from a Q2 observation cohort.

    ``candidate_symbols`` uses only fields exposed by Q2. The t1-v2 A2 writer
    also checks its internal ``auction.ts_ms``, which Q2 does not expose; this
    projection therefore records the candidate rule and never claims complete
    market-universe coverage. Quotes without a source timestamp, or whose
    source timestamp belongs to another Shanghai trade date, are retained as
    unknown membership rather than included as target-date facts.
    """

    trade_date: str
    status: FactStatus
    source_id: str
    source_table: str
    observation_time_ms: Optional[int]
    input_symbol_count: int
    expected_symbol_count: Optional[int]
    missing_symbols: Tuple[str, ...]
    unexpected_symbols: Tuple[str, ...]
    source_time_unknown_symbols: Tuple[str, ...]
    source_date_mismatch_symbols: Tuple[str, ...]
    candidate_symbols: Tuple[str, ...]
    candidate_membership_unknown_symbols: Tuple[str, ...]
    candidate_rule: str
    candidate_rule_status: str
    market_universe_coverage_status: str
    q2_input_coverage: Optional[float]
    metrics: Mapping[str, Optional[int]]
    metric_unknown_counts: Mapping[str, int]
    invalid_fields: Tuple[str, ...]
    input_content_hash: Optional[str]
    evidence_refs: Tuple[str, ...] = ()
    content_hash: str = field(init=False)
    evidence_hash: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "missing_symbols", tuple(sorted(set(self.missing_symbols))))
        object.__setattr__(self, "unexpected_symbols", tuple(sorted(set(self.unexpected_symbols))))
        object.__setattr__(
            self,
            "source_time_unknown_symbols",
            tuple(sorted(set(self.source_time_unknown_symbols))),
        )
        object.__setattr__(
            self,
            "source_date_mismatch_symbols",
            tuple(sorted(set(self.source_date_mismatch_symbols))),
        )
        object.__setattr__(self, "candidate_symbols", tuple(sorted(set(self.candidate_symbols))))
        object.__setattr__(
            self,
            "candidate_membership_unknown_symbols",
            tuple(sorted(set(self.candidate_membership_unknown_symbols))),
        )
        object.__setattr__(self, "metrics", deep_freeze(self.metrics))
        object.__setattr__(self, "metric_unknown_counts", deep_freeze(self.metric_unknown_counts))
        object.__setattr__(self, "invalid_fields", tuple(sorted(set(self.invalid_fields))))
        object.__setattr__(self, "evidence_refs", tuple(sorted(set(self.evidence_refs))))
        semantic_payload = {
            "contract_version": Q2_AUCTION_SUMMARY_CONTRACT_VERSION,
            "trade_date": self.trade_date,
            "status": self.status,
            "input_symbol_count": self.input_symbol_count,
            "expected_symbol_count": self.expected_symbol_count,
            "missing_symbols": self.missing_symbols,
            "unexpected_symbols": self.unexpected_symbols,
            "source_time_unknown_symbols": self.source_time_unknown_symbols,
            "source_date_mismatch_symbols": self.source_date_mismatch_symbols,
            "candidate_symbols": self.candidate_symbols,
            "candidate_membership_unknown_symbols": self.candidate_membership_unknown_symbols,
            "candidate_rule": self.candidate_rule,
            "candidate_rule_status": self.candidate_rule_status,
            "market_universe_coverage_status": self.market_universe_coverage_status,
            "metrics": self.metrics,
            "metric_unknown_counts": self.metric_unknown_counts,
            "invalid_fields": self.invalid_fields,
        }
        evidence_payload = {
            "contract_version": Q2_AUCTION_SUMMARY_CONTRACT_VERSION,
            "source_id": self.source_id,
            "source_table": self.source_table,
            "observation_time_ms": self.observation_time_ms,
            "q2_input_coverage": self.q2_input_coverage,
            "input_content_hash": self.input_content_hash,
            "evidence_refs": self.evidence_refs,
            "hash_contract_versions": {
                "semantic": SEMANTIC_HASH_CONTRACT_VERSION,
                "evidence": EVIDENCE_HASH_CONTRACT_VERSION,
            },
        }
        object.__setattr__(self, "content_hash", semantic_hash(semantic_payload))
        object.__setattr__(self, "evidence_hash", evidence_hash(evidence_payload))

    @property
    def candidate_count(self) -> int:
        return len(self.candidate_symbols)

    @property
    def candidate_membership_unknown_count(self) -> int:
        return len(self.candidate_membership_unknown_symbols)

    def as_mapping(self) -> Mapping[str, Any]:
        return {
            "trade_date": self.trade_date,
            "status": self.status,
            "source_id": self.source_id,
            "source_table": self.source_table,
            "observation_time_ms": self.observation_time_ms,
            "input_symbol_count": self.input_symbol_count,
            "expected_symbol_count": self.expected_symbol_count,
            "missing_symbols": self.missing_symbols,
            "unexpected_symbols": self.unexpected_symbols,
            "source_time_unknown_symbols": self.source_time_unknown_symbols,
            "source_date_mismatch_symbols": self.source_date_mismatch_symbols,
            "candidate_count": self.candidate_count,
            "candidate_symbols": self.candidate_symbols,
            "candidate_membership_unknown_count": self.candidate_membership_unknown_count,
            "candidate_membership_unknown_symbols": self.candidate_membership_unknown_symbols,
            "candidate_rule": self.candidate_rule,
            "candidate_rule_status": self.candidate_rule_status,
            "market_universe_coverage_status": self.market_universe_coverage_status,
            "q2_input_coverage": self.q2_input_coverage,
            "metrics": self.metrics,
            "metric_unknown_counts": self.metric_unknown_counts,
            "invalid_fields": self.invalid_fields,
            "input_content_hash": self.input_content_hash,
            "content_hash": self.content_hash,
            "evidence_hash": self.evidence_hash,
            "evidence_refs": self.evidence_refs,
        }


def derive_q2_auction_summary(
    quotes: Mapping[str, Q2Quote],
    *,
    trade_date: str,
    source_id: str,
    source_table: str,
    expected_symbols: Optional[Sequence[str]] = None,
    observation_time_ms: Optional[int] = None,
    input_content_hash: Optional[str] = None,
    evidence_refs: Tuple[str, ...] = (),
) -> Q2AuctionSummaryProjection:
    """Derive fact-only 09:25 aggregates from an already-read Q2 cohort.

    This function has no Redis/TD/Rabbit access. Candidate membership is
    inferred from non-zero Q2 ``am``/``br``/``ar`` values. When these fields
    are absent or invalid and no positive value establishes membership, the
    symbol remains UNKNOWN instead of being treated as a zero-valued exclusion.
    Source timestamps missing or outside ``trade_date`` also remain UNKNOWN
    and do not contribute numeric values to the requested day's summary.
    """

    _validate_trade_date(trade_date)
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError("source_id is required")
    if not isinstance(source_table, str) or not source_table.strip():
        raise ValueError("source_table is required")
    _validate_timestamp("observation_time_ms", observation_time_ms)
    if not isinstance(quotes, Mapping):
        raise TypeError("quotes must be a symbol-to-Q2Quote mapping")
    if isinstance(expected_symbols, (str, bytes)):
        raise TypeError("expected_symbols must be a sequence of symbols")

    normalized_quotes: Dict[str, Q2Quote] = {}
    for raw_symbol, quote in quotes.items():
        symbol = normalize_symbol(raw_symbol)
        if not isinstance(quote, Q2Quote):
            raise TypeError("quotes must contain Q2Quote values")
        if quote.symbol != symbol:
            raise ValueError("Q2 quote symbol does not match its mapping key")
        normalized_quotes[symbol] = quote

    expected = (
        tuple(sorted({normalize_symbol(symbol) for symbol in expected_symbols}))
        if expected_symbols is not None
        else None
    )
    invalid_fields: list[str] = []
    parsed: Dict[str, Dict[str, Tuple[Optional[int], str]]] = {}
    source_time_unknown: list[str] = []
    source_date_mismatch: list[str] = []
    for symbol, quote in normalized_quotes.items():
        source_time_ms = quote.source_record_time_ms
        if source_time_ms is None:
            source_time_unknown.append(symbol)
            if "ts" in quote.field_errors:
                invalid_fields.append("%s.ts" % symbol)
            continue
        source_date = datetime.fromtimestamp(
            source_time_ms / 1000.0,
            tz=timezone.utc,
        ).astimezone(_SHANGHAI).date().isoformat()
        if source_date != trade_date:
            source_date_mismatch.append(symbol)
            continue
        parsed[symbol] = {}
        for field_name in ("am", "br", "ar", "a25", "pc", "ls"):
            value, quality = _q2_integer_field(quote, field_name)
            parsed[symbol][field_name] = (value, quality)
            if quality == "INVALID":
                invalid_fields.append("%s.%s" % (symbol, field_name))

    missing_symbols = tuple(sorted(set(expected or ()) - set(parsed)))
    unexpected_symbols = (
        tuple(sorted(set(parsed) - set(expected)))
        if expected is not None
        else ()
    )
    if expected is None:
        q2_coverage = None
    elif expected:
        q2_coverage = len(set(expected) & set(parsed)) / float(len(expected))
    else:
        q2_coverage = 1.0 if not parsed else 0.0

    candidates: list[str] = []
    uncertain_candidates: list[str] = [
        *missing_symbols,
        *source_time_unknown,
        *source_date_mismatch,
    ]
    for symbol, fields in parsed.items():
        auction_values = [fields[name] for name in ("am", "br", "ar")]
        has_positive = any(
            quality == "PRESENT_VALUE" and value is not None and value > 0
            for value, quality in auction_values
        )
        all_known = all(
            quality == "PRESENT_VALUE" and value is not None and value >= 0
            for value, quality in auction_values
        )
        if has_positive:
            candidates.append(symbol)
        elif all_known:
            continue
        else:
            uncertain_candidates.append(symbol)

    candidate_unknown_count = len(set(uncertain_candidates))
    rows = [parsed[symbol] for symbol in candidates]
    unknown_counts: Dict[str, int] = {}
    metrics: Dict[str, Optional[int]] = {}

    def set_metric(name: str, known_value: int, field_unknown_count: int = 0) -> None:
        unknown_count = candidate_unknown_count + field_unknown_count
        unknown_counts[name] = unknown_count
        metrics[name] = known_value if unknown_count == 0 else None

    set_metric("stock_count", len(candidates))

    anchor_unknown = sum(fields["a25"][1] != "PRESENT_VALUE" for fields in rows)
    anchor_values = [fields["a25"][0] for fields in rows]
    if any(value is not None and value < 0 for value in anchor_values):
        anchor_unknown += sum(value is not None and value < 0 for value in anchor_values)
    valid_count = sum(value is not None and value > 0 for value in anchor_values)
    unavailable_count = sum(value == 0 for value in anchor_values)
    set_metric("valid_stock_count", valid_count, anchor_unknown)
    set_metric("unavailable_stock_count", unavailable_count, anchor_unknown)

    breadth_unknown = anchor_unknown
    positive = negative = flat = 0
    for fields in rows:
        price, price_quality = fields["a25"]
        if price_quality != "PRESENT_VALUE" or price is None or price < 0:
            continue
        if price == 0:
            continue
        previous_close, pc_quality = fields["pc"]
        if pc_quality != "PRESENT_VALUE" or previous_close is None:
            breadth_unknown += 1
        elif previous_close < 0:
            breadth_unknown += 1
        elif previous_close == 0:
            continue
        else:
            change_bp = trunc_div((price - previous_close) * 10000, previous_close)
            positive += change_bp > 0
            negative += change_bp < 0
            flat += change_bp == 0
    set_metric("positive_count", positive, breadth_unknown - anchor_unknown)
    set_metric("negative_count", negative, breadth_unknown - anchor_unknown)
    set_metric("flat_count", flat, breadth_unknown - anchor_unknown)

    auction_amount_unknown = sum(
        fields["am"][1] != "PRESENT_VALUE" or fields["am"][0] is None or fields["am"][0] < 0
        for fields in rows
    )
    auction_amount = sum(
        fields["am"][0] or 0
        for fields in rows
        if fields["am"][1] == "PRESENT_VALUE"
        and fields["am"][0] is not None
        and fields["am"][0] >= 0
    )
    set_metric("auction_amount_yuan", auction_amount, auction_amount_unknown)

    limit_state_unknown = sum(
        fields["ls"][1] != "PRESENT_VALUE" or fields["ls"][0] not in (-1, 0, 1)
        for fields in rows
    )
    limit_up = sum(fields["ls"][0] == 1 for fields in rows if fields["ls"][1] == "PRESENT_VALUE")
    limit_down = sum(fields["ls"][0] == -1 for fields in rows if fields["ls"][1] == "PRESENT_VALUE")
    set_metric("limit_up_count", limit_up, limit_state_unknown)
    set_metric("limit_down_count", limit_down, limit_state_unknown)

    seal_unknown = limit_state_unknown + sum(
        fields["ls"][1] == "PRESENT_VALUE"
        and fields["ls"][0] == 1
        and (fields["br"][1] != "PRESENT_VALUE" or fields["br"][0] is None or fields["br"][0] < 0)
        for fields in rows
    )
    seal_amount = sum(
        fields["br"][0]
        for fields in rows
        if fields["ls"] == (1, "PRESENT_VALUE")
        and fields["br"][1] == "PRESENT_VALUE"
        and fields["br"][0] is not None
        and fields["br"][0] >= 0
    )
    set_metric("limit_up_seal_amount_yuan", seal_amount, seal_unknown)

    if not normalized_quotes and not expected:
        status = FactStatus.UNAVAILABLE
    elif unexpected_symbols or any(unknown_counts[name] for name in _Q2_SUMMARY_METRICS):
        status = FactStatus.PARTIAL
    else:
        status = FactStatus.READY

    return Q2AuctionSummaryProjection(
        trade_date=trade_date,
        status=status,
        source_id=source_id.strip(),
        source_table=source_table.strip(),
        observation_time_ms=observation_time_ms,
        input_symbol_count=len(normalized_quotes),
        expected_symbol_count=len(expected) if expected is not None else None,
        missing_symbols=missing_symbols,
        unexpected_symbols=unexpected_symbols,
        source_time_unknown_symbols=tuple(source_time_unknown),
        source_date_mismatch_symbols=tuple(source_date_mismatch),
        candidate_symbols=tuple(candidates),
        candidate_membership_unknown_symbols=tuple(uncertain_candidates),
        candidate_rule=Q2_AUCTION_CANDIDATE_RULE,
        candidate_rule_status="Q2_VALUE_DERIVED",
        market_universe_coverage_status="UNKNOWN",
        q2_input_coverage=q2_coverage,
        metrics=metrics,
        metric_unknown_counts=unknown_counts,
        invalid_fields=tuple(invalid_fields),
        input_content_hash=input_content_hash,
        evidence_refs=evidence_refs,
    )


def _q2_integer_field(quote: Q2Quote, name: str) -> Tuple[Optional[int], str]:
    if name in quote.field_errors:
        return None, "INVALID"
    if name not in quote.raw_fields:
        return None, "MISSING"
    raw = quote.raw_fields.get(name)
    if raw in (None, "") or isinstance(raw, bool):
        return None, "MISSING" if raw in (None, "") else "INVALID"
    text = str(raw).strip()
    if not re.fullmatch(r"[+-]?\d+", text):
        return None, "INVALID"
    value = int(text)
    if name in ("am", "br", "ar", "a25", "pc") and value < 0:
        return None, "INVALID"
    if name == "ls" and value not in (-1, 0, 1):
        return None, "INVALID"
    return value, "PRESENT_VALUE"
def normalize_auction_market_summary(
    raw: Mapping[str, Any],
    *,
    trade_date: str,
    source_id: str,
    source_table: str = "redis:market:auction:0925",
    observation_time_ms: Optional[int] = None,
    evidence_refs: Tuple[str, ...] = (),
) -> AuctionMarketSummaryFact:
    """Normalize the legacy Redis A2 summary without inventing values.

    The legacy aliases are accepted only at this boundary.  All downstream
    fields use explicit canonical names and yuan/count units.  A missing field
    remains ``None``; an explicit numeric zero is preserved as zero.
    """

    if not isinstance(raw, Mapping):
        raise TypeError("auction market summary raw value must be a mapping")
    _validate_trade_date(trade_date)
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError("source_id is required")
    if not isinstance(source_table, str) or not source_table.strip():
        raise ValueError("source_table is required")
    _validate_timestamp("observation_time_ms", observation_time_ms)

    values: dict[str, Optional[int]] = {}
    missing: list[str] = []
    invalid: list[str] = []
    for canonical_name, raw_name in _FIELD_SPECS:
        if raw_name not in raw or raw.get(raw_name) in (None, ""):
            values[canonical_name] = None
            missing.append(canonical_name)
            continue
        parsed = _nonnegative_int(raw.get(raw_name))
        if parsed is None:
            values[canonical_name] = None
            invalid.append(canonical_name)
        else:
            values[canonical_name] = parsed

    if not raw:
        status = FactStatus.UNAVAILABLE
    elif invalid:
        status = FactStatus.INVALID
    elif missing:
        status = FactStatus.PARTIAL
    else:
        status = FactStatus.READY
    return AuctionMarketSummaryFact(
        trade_date=trade_date,
        status=status,
        source_id=source_id.strip(),
        source="a2_0925_summary",
        source_table=source_table,
        observation_time_ms=observation_time_ms,
        **values,
        missing_fields=tuple(missing),
        invalid_fields=tuple(invalid),
        evidence_refs=evidence_refs,
    )


def _validate_trade_date(value: str) -> None:
    if not isinstance(value, str) or not _STRICT_TRADE_DATE.fullmatch(value):
        raise ValueError("trade_date must use strict YYYY-MM-DD form")
    if date.fromisoformat(value).isoformat() != value:
        raise ValueError("trade_date must use strict YYYY-MM-DD form")


def _validate_timestamp(name: str, value: Optional[int]) -> None:
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
    ):
        raise ValueError(f"{name} must be a positive epoch-millisecond integer")


def _nonnegative_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if isinstance(value, float) and value != number:
        return None
    if isinstance(value, str) and str(number) != value.strip():
        return None
    return number if number >= 0 else None


__all__ = [
    "AUCTION_MARKET_SUMMARY_CONTRACT_VERSION",
    "AuctionMarketSummaryFact",
    "normalize_auction_market_summary",
]
