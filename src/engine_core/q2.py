"""Thin adapter for the existing Redis Q2 projection."""

from __future__ import annotations

import re
import math
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Protocol, Sequence, Tuple
from zoneinfo import ZoneInfo

from .contracts import (
    DataStatus,
    MarketDataEnvelope,
    PayloadKind,
    Provenance,
    deep_freeze,
    semantic_hash,
)


@dataclass(frozen=True)
class FreshnessPolicy:
    """Explicit source-time policy; production callers must configure it."""

    stale_after_ms: Optional[int] = None
    max_future_skew_ms: int = 0

    def __post_init__(self) -> None:
        if self.stale_after_ms is not None and self.stale_after_ms < 0:
            raise ValueError("stale_after_ms must be non-negative")
        if self.max_future_skew_ms < 0:
            raise ValueError("max_future_skew_ms must be non-negative")


@dataclass(frozen=True)
class Q2FieldSpec:
    raw_name: str
    canonical_name: str
    value_type: str
    unit: str
    semantic: str
    required: bool


Q2_FIELD_CONTRACT: Tuple[Q2FieldSpec, ...] = (
    Q2FieldSpec("px", "price_milli", "int", "milli_price", "current price", True),
    Q2FieldSpec("pc", "pre_close_milli", "int", "milli_price", "previous close", True),
    Q2FieldSpec("amt", "amount_native", "int", "source_native", "cumulative amount", False),
    Q2FieldSpec("vol", "volume_native", "int", "source_native", "cumulative volume", False),
    Q2FieldSpec("ts", "source_timestamp_ms", "epoch_ms", "epoch_ms", "source update time", True),
    Q2FieldSpec("ph", "phase", "int", "code", "market phase", False),
    Q2FieldSpec("br", "auction_bid_amount_native", "int", "source_native", "resting bid amount", False),
    Q2FieldSpec("ar", "auction_ask_amount_native", "int", "source_native", "resting ask amount", False),
    Q2FieldSpec("mk", "market", "str", "code", "market code", False),
)

Q2_OBSERVED_OPTIONAL_FIELDS: Tuple[str, ...] = (
    "iv", "ia", "ln", "ls", "mx", "mn", "spd1m", "amt2m", "amt5m",
    "vec3m", "vec5m", "a20", "a24", "a25", "am",
)
SHANGHAI = ZoneInfo("Asia/Shanghai")


class Q2RedisClient(Protocol):
    def smembers(self, key: str) -> Sequence[Any]:
        ...

    def hgetall(self, key: str) -> Mapping[Any, Any]:
        ...


def _decode(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="strict")
    return value


def normalize_symbol(value: Any) -> str:
    """Normalize a six-digit Q2 symbol without stripping qualified symbols."""

    text = str(_decode(value)).strip().upper()
    if re.fullmatch(r"\d{6}", text):
        return text
    if re.fullmatch(r"[A-Z]{2}\.\d{6}", text):
        return text
    raise ValueError("invalid Q2 symbol: %s" % text)


def _to_int(value: Any) -> Optional[int]:
    value = _decode(value)
    if value is None or value == "":
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _epoch_ms(value: Optional[int]) -> Optional[int]:
    if value is None:
        return None
    min_epoch_ms = 946_684_800_000
    max_epoch_ms = 4_102_444_800_000
    if min_epoch_ms <= value <= max_epoch_ms:
        return value
    min_epoch_seconds = min_epoch_ms // 1000
    max_epoch_seconds = max_epoch_ms // 1000
    if min_epoch_seconds <= value <= max_epoch_seconds:
        return value * 1000
    return None


@dataclass(frozen=True)
class Q2Quote:
    symbol: str
    market: Optional[str]
    name: Optional[str]
    price_milli: Optional[int]
    pre_close_milli: Optional[int]
    amount_native: Optional[int]
    volume_native: Optional[int]
    source_timestamp_ms: Optional[int]
    phase: Optional[int]
    limit_state: Optional[int]
    auction_amount_native: Optional[int]
    auction_bid_amount_native: Optional[int]
    auction_ask_amount_native: Optional[int]
    amount_2m_native: Optional[int]
    speed_1m_bp: Optional[int]
    raw_fields: Mapping[str, Any]
    field_errors: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "raw_fields", deep_freeze(self.raw_fields))
        object.__setattr__(self, "field_errors", tuple(self.field_errors))

    def to_mapping(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "market": self.market,
            "name": self.name,
            "price_milli": self.price_milli,
            "pre_close_milli": self.pre_close_milli,
            "amount_native": self.amount_native,
            "volume_native": self.volume_native,
            "source_timestamp_ms": self.source_timestamp_ms,
            "phase": self.phase,
            "limit_state": self.limit_state,
            "auction_amount_native": self.auction_amount_native,
            "auction_bid_amount_native": self.auction_bid_amount_native,
            "auction_ask_amount_native": self.auction_ask_amount_native,
            "amount_2m_native": self.amount_2m_native,
            "speed_1m_bp": self.speed_1m_bp,
            "field_errors": self.field_errors,
        }


def normalize_q2(symbol: str, raw_hash: Mapping[Any, Any]) -> Q2Quote:
    """Normalize one legacy Redis hash without applying business defaults.

    ``raw_fields`` is retained for evidence only and is intentionally absent
    from ``Q2Quote.to_mapping``.  Unknown producer fields therefore do not
    silently become canonical business inputs.
    """

    normalized_symbol = normalize_symbol(symbol)
    raw = {str(_decode(key)): _decode(value) for key, value in raw_hash.items()}
    errors = []

    def int_field(name: str) -> Optional[int]:
        value = _to_int(raw.get(name))
        if name in raw and raw.get(name) not in (None, "") and value is None:
            errors.append(name)
        return value

    timestamp_raw = int_field("ts")
    timestamp_ms = _epoch_ms(timestamp_raw)
    if timestamp_raw is not None and timestamp_ms is None:
        errors.append("ts")
    quote = Q2Quote(
        symbol=normalized_symbol,
        market=str(raw["mk"]).strip().lower() if raw.get("mk") is not None else None,
        name=str(raw["name"]) if raw.get("name") is not None else None,
        price_milli=int_field("px"),
        pre_close_milli=int_field("pc"),
        amount_native=int_field("amt"),
        volume_native=int_field("vol"),
        source_timestamp_ms=timestamp_ms,
        phase=int_field("ph"),
        limit_state=int_field("ls"),
        auction_amount_native=int_field("am"),
        auction_bid_amount_native=int_field("br"),
        auction_ask_amount_native=int_field("ar"),
        amount_2m_native=int_field("amt2m"),
        speed_1m_bp=int_field("spd1m"),
        raw_fields=raw,
        field_errors=tuple(sorted(set(errors))),
    )
    return quote


def classify_equity(symbol: str, quote: Q2Quote | Mapping[str, Any]) -> bool:
    """Apply the existing stock/universe filter, excluding index-like rows."""

    normalized = normalize_symbol(symbol)
    market = (
        quote.market
        if isinstance(quote, Q2Quote)
        else quote.get("market", quote.get("mk"))
    )
    market_code = str(market or "").strip().lower()
    if market_code == "sz":
        return normalized.startswith(("000", "001", "002", "003", "300", "301"))
    if market_code == "kc":
        return normalized.startswith(("688", "689"))
    if market_code == "sh":
        return normalized.startswith(("600", "601", "603", "605", "688", "689"))
    price_milli = (
        quote.price_milli
        if isinstance(quote, Q2Quote)
        else _to_int(quote.get("price_milli", quote.get("px")))
    )
    if normalized.startswith(("000", "001", "002", "003", "300", "301")) and (price_milli or 0) >= 1_000_000:
        return False
    return True


def validate_q2(
    quote: Q2Quote,
    *,
    observed_at_ms: int,
    trade_date: Optional[str] = None,
    freshness_policy: FreshnessPolicy = FreshnessPolicy(),
) -> Tuple[str, ...]:
    """Return deterministic validation/freshness issue codes for one quote."""

    errors = list(quote.field_errors)
    if quote.price_milli is None:
        errors.append("px")
    elif quote.price_milli <= 0:
        errors.append("px_non_positive")
    if quote.pre_close_milli is None:
        errors.append("pc")
    elif quote.pre_close_milli <= 0:
        errors.append("pc_non_positive")
    if quote.source_timestamp_ms is None:
        errors.append("ts")
    else:
        if quote.source_timestamp_ms > observed_at_ms + freshness_policy.max_future_skew_ms:
            errors.append("future_ts")
        if freshness_policy.stale_after_ms is not None and (
            observed_at_ms - quote.source_timestamp_ms > freshness_policy.stale_after_ms
        ):
            errors.append("stale")
        if trade_date is not None:
            source_date = datetime.fromtimestamp(
                quote.source_timestamp_ms / 1000.0,
                tz=timezone.utc,
            ).astimezone(SHANGHAI).date().isoformat()
            if source_date != trade_date:
                errors.append("trade_date")
    return tuple(sorted(set(errors)))


@dataclass(frozen=True)
class Q2ProjectionSnapshot:
    trade_date: str
    envelope: MarketDataEnvelope
    quotes: Mapping[str, Q2Quote]
    expected_symbols: Tuple[str, ...]
    missing_symbols: Tuple[str, ...]
    stale_symbols: Tuple[str, ...]
    coverage: float
    status: DataStatus
    consistency_status: str
    oldest_source_time_ms: Optional[int]
    newest_source_time_ms: Optional[int]
    content_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "quotes", deep_freeze(self.quotes))
        object.__setattr__(self, "expected_symbols", tuple(self.expected_symbols))
        object.__setattr__(self, "missing_symbols", tuple(self.missing_symbols))
        object.__setattr__(self, "stale_symbols", tuple(self.stale_symbols))


def build_q2_projection(
    trade_date: str,
    observed_at: datetime,
    expected_symbols: Sequence[Any],
    raw_hashes: Mapping[str, Mapping[Any, Any]],
    *,
    freshness_policy: FreshnessPolicy = FreshnessPolicy(),
    source_id: str = "redis_q2_projection",
) -> Q2ProjectionSnapshot:
    """Build a projection from already-read hashes; no Redis access occurs."""

    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    observed_ms = int(observed_at.astimezone(timezone.utc).timestamp() * 1000)
    expected = tuple(sorted({normalize_symbol(item) for item in expected_symbols}))
    quotes: Dict[str, Q2Quote] = {}
    missing = []
    stale = []
    for symbol in expected:
        raw_hash = raw_hashes.get(symbol)
        if not raw_hash:
            missing.append(symbol)
            continue
        quote = normalize_q2(symbol, raw_hash)
        validation_errors = validate_q2(
            quote,
            observed_at_ms=observed_ms,
            trade_date=trade_date,
            freshness_policy=freshness_policy,
        )
        if validation_errors:
            quote = replace(quote, field_errors=validation_errors)
        if "stale" in validation_errors:
            stale.append(symbol)
        quotes[symbol] = quote

    source_times = [
        quote.source_timestamp_ms
        for quote in quotes.values()
        if quote.source_timestamp_ms is not None
    ]
    oldest = min(source_times) if source_times else None
    newest = max(source_times) if source_times else None
    coverage = len(quotes) / float(len(expected)) if expected else 0.0
    has_non_stale_errors = any(
        any(error != "stale" for error in quote.field_errors)
        for quote in quotes.values()
    )
    if not expected:
        status = DataStatus.MISSING
        consistency = "EMPTY_UNIVERSE"
    elif missing or has_non_stale_errors:
        status = DataStatus.PARTIAL
        consistency = "BEST_EFFORT_PARTIAL"
    elif stale and len(stale) == len(quotes):
        status = DataStatus.STALE
        consistency = "BEST_EFFORT_STALE"
    elif stale:
        status = DataStatus.PARTIAL
        consistency = "BEST_EFFORT_MIXED_FRESHNESS"
    else:
        status = DataStatus.READY
        consistency = "BEST_EFFORT"

    quote_payload = {
        symbol: quotes[symbol].to_mapping()
        for symbol in sorted(quotes)
    }
    content = {
        "trade_date": trade_date,
        "expected_symbols": expected,
        "missing_symbols": tuple(missing),
        "stale_symbols": tuple(sorted(stale)),
        "quotes": quote_payload,
    }
    content_digest = semantic_hash(content, schema_version=1)
    provenance = Provenance(
        source_id=source_id,
        source_kind="redis_projection",
        source_schema="Q2RedisHashV1",
        source_trade_date=trade_date,
        effective_at_ms=newest,
        observed_at_ms=observed_ms,
        notes=(consistency,),
    )
    envelope = MarketDataEnvelope(
        envelope_id=semantic_hash(
            {
                "source_id": source_id,
                "trade_date": trade_date,
                "effective_ms": newest,
                "content_hash": content_digest,
            },
            schema_version=1,
        ),
        payload_kind=PayloadKind.L2_PROJECTION_SNAPSHOT,
        source_id=source_id,
        schema_version=1,
        effective_time_ms=newest,
        observed_time_ms=observed_ms,
        generation=None,
        generation_kind="OBSERVATION_COHORT",
        payload=content,
        provenance=provenance,
    )
    return Q2ProjectionSnapshot(
        trade_date=trade_date,
        envelope=envelope,
        quotes=quotes,
        expected_symbols=expected,
        missing_symbols=tuple(missing),
        stale_symbols=tuple(sorted(stale)),
        coverage=coverage,
        status=status,
        consistency_status=consistency,
        oldest_source_time_ms=oldest,
        newest_source_time_ms=newest,
        content_hash=content_digest,
    )


class RedisQ2ProjectionAdapter:
    """Read-only adapter for q2 active membership and per-symbol hashes.

    The adapter never writes Redis and never invents a global generation.  A
    read cohort is identified by its observation time and content hash.
    """

    def __init__(
        self,
        client: Q2RedisClient,
        q2_prefix: str = "q2:",
        active_prefix: str = "q2:active:",
        source_id: str = "redis_q2_projection",
    ) -> None:
        self._client = client
        self._q2_prefix = q2_prefix
        self._active_prefix = active_prefix
        self._source_id = source_id

    def read(
        self,
        trade_date: str,
        observed_at: datetime,
        *,
        freshness_policy: Optional[FreshnessPolicy] = None,
        stale_after_ms: Optional[int] = None,
    ) -> Q2ProjectionSnapshot:
        """Read one best-effort Q2 projection cohort.

        observed_at must be timezone-aware.  Missing hashes are reported in
        the result; they are never represented as zero-valued quotes.
        """

        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        if freshness_policy is not None and stale_after_ms is not None:
            raise ValueError("pass freshness_policy or stale_after_ms, not both")
        policy = freshness_policy or FreshnessPolicy(stale_after_ms=stale_after_ms)

        raw_symbols = self._client.smembers(self._active_prefix + trade_date)
        expected = tuple(sorted({normalize_symbol(item) for item in raw_symbols}))
        raw_hashes = {}
        for symbol in expected:
            raw_hash = self._client.hgetall(self._q2_prefix + symbol)
            if raw_hash:
                raw_hashes[symbol] = raw_hash
        return build_q2_projection(
            trade_date,
            observed_at,
            expected,
            raw_hashes,
            freshness_policy=policy,
            source_id=self._source_id,
        )
