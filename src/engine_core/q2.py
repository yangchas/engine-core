"""Thin adapter for the existing Redis Q2 projection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Protocol, Sequence, Tuple

from .contracts import (
    DataStatus,
    MarketDataEnvelope,
    PayloadKind,
    Provenance,
    canonical_hash,
)


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


def _to_float(value: Any) -> Optional[float]:
    value = _decode(value)
    if value is None or value == "":
        return None
    try:
        return float(str(value))
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
    amount_native: Optional[float]
    volume_native: Optional[float]
    source_timestamp_ms: Optional[int]
    phase: Optional[int]
    limit_state: Optional[int]
    auction_amount_native: Optional[float]
    auction_bid_amount_native: Optional[float]
    auction_ask_amount_native: Optional[float]
    amount_2m_native: Optional[float]
    speed_1m_bp: Optional[int]
    raw_fields: Mapping[str, Any]
    field_errors: Tuple[str, ...] = ()

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
            "raw_fields": dict(self.raw_fields),
            "field_errors": self.field_errors,
        }


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
        stale_after_ms: Optional[int] = None,
    ) -> Q2ProjectionSnapshot:
        """Read one best-effort Q2 projection cohort.

        observed_at must be timezone-aware.  Missing hashes are reported in
        the result; they are never represented as zero-valued quotes.
        """

        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        observed_ms = int(observed_at.astimezone(timezone.utc).timestamp() * 1000)

        raw_symbols = self._client.smembers(self._active_prefix + trade_date)
        expected = tuple(sorted({normalize_symbol(item) for item in raw_symbols}))
        quotes: Dict[str, Q2Quote] = {}
        missing = []

        for symbol in expected:
            raw_hash = self._client.hgetall(self._q2_prefix + symbol)
            if not raw_hash:
                missing.append(symbol)
                continue
            quote = self._parse_quote(symbol, raw_hash)
            quotes[symbol] = quote

        source_times = [
            quote.source_timestamp_ms
            for quote in quotes.values()
            if quote.source_timestamp_ms is not None
        ]
        oldest = min(source_times) if source_times else None
        newest = max(source_times) if source_times else None
        stale = []
        if stale_after_ms is not None:
            stale = [
                symbol
                for symbol, quote in quotes.items()
                if quote.source_timestamp_ms is not None
                and observed_ms - quote.source_timestamp_ms > stale_after_ms
            ]

        coverage = (len(quotes) / float(len(expected))) if expected else 0.0
        if not expected:
            status = DataStatus.MISSING
            consistency = "EMPTY_UNIVERSE"
        elif missing:
            status = DataStatus.PARTIAL
            consistency = "BEST_EFFORT_PARTIAL"
        elif any(quote.field_errors for quote in quotes.values()):
            status = DataStatus.PARTIAL
            consistency = "BEST_EFFORT_FIELD_ERRORS"
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
        content_digest = canonical_hash(content)
        effective_ms = newest if newest is not None else observed_ms
        provenance = Provenance(
            source_id=self._source_id,
            source_kind="redis_projection",
            source_schema="Q2RedisHashV1",
            source_trade_date=trade_date,
            effective_at_ms=effective_ms,
            observed_at_ms=observed_ms,
            notes=(consistency,),
        )
        envelope = MarketDataEnvelope(
            envelope_id=canonical_hash(
                {
                    "source_id": self._source_id,
                    "trade_date": trade_date,
                    "effective_ms": effective_ms,
                    "content_hash": content_digest,
                }
            ),
            payload_kind=PayloadKind.L2_PROJECTION_SNAPSHOT,
            source_id=self._source_id,
            schema_version=1,
            effective_time_ms=effective_ms,
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

    @staticmethod
    def _parse_quote(symbol: str, raw_hash: Mapping[Any, Any]) -> Q2Quote:
        raw = {str(_decode(key)): _decode(value) for key, value in raw_hash.items()}
        errors = []

        def int_field(name: str) -> Optional[int]:
            value = _to_int(raw.get(name))
            if name in raw and raw.get(name) not in (None, "") and value is None:
                errors.append(name)
            return value

        def float_field(name: str) -> Optional[float]:
            value = _to_float(raw.get(name))
            if name in raw and raw.get(name) not in (None, "") and value is None:
                errors.append(name)
            return value

        timestamp_raw = int_field("ts")
        timestamp_ms = _epoch_ms(timestamp_raw)
        if timestamp_raw is not None and timestamp_ms is None:
            errors.append("ts")
        return Q2Quote(
            symbol=symbol,
            market=str(raw["mk"]) if raw.get("mk") is not None else None,
            name=str(raw["name"]) if raw.get("name") is not None else None,
            price_milli=int_field("px"),
            pre_close_milli=int_field("pc"),
            amount_native=float_field("amt"),
            volume_native=float_field("vol"),
            source_timestamp_ms=timestamp_ms,
            phase=int_field("ph"),
            limit_state=int_field("ls"),
            auction_amount_native=float_field("am"),
            auction_bid_amount_native=float_field("br"),
            auction_ask_amount_native=float_field("ar"),
            amount_2m_native=float_field("amt2m"),
            speed_1m_bp=int_field("spd1m"),
            raw_fields=raw,
            field_errors=tuple(sorted(set(errors))),
        )
