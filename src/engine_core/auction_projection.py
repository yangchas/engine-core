"""Read-only normalization for the legacy Redis auction projection.

The deployed ``engine-next`` loader reads only the ``summary`` and
``top_amount`` fields from ``market:auction:{date}:{tag}``.  This module
extracts that narrow access contract without importing the legacy runtime,
performing recovery, falling back to network sources, or writing Redis.

The result is deliberately a *projection* rather than a full-universe
auction fact.  A requested symbol that is absent from ``top_amount`` remains
absent; callers must not interpret that as zero or as proof that the symbol
was not present in the market.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Mapping, Protocol, Sequence

from .auction import normalize_auction_change_ratio
from .contracts import (
    EVIDENCE_HASH_CONTRACT_VERSION,
    SEMANTIC_HASH_CONTRACT_VERSION,
    deep_freeze,
    evidence_hash,
    semantic_hash,
)


REDIS_AUCTION_PROJECTION_CONTRACT_VERSION = "RedisAuctionProjectionV1"
REDIS_AUCTION_PROJECTION_SCOPE = "TOP_AMOUNT"
DEFAULT_AUCTION_TAGS = ("0920", "0924", "0925")
_SYMBOL_RE = re.compile(r"^\d{6}$")
_TAG_RE = re.compile(r"^\d{4}$")


class RedisAuctionProjectionClient(Protocol):
    """The minimum read-only Redis surface needed by this adapter."""

    def hgetall(self, key: str) -> Mapping[Any, Any]:
        ...


def _decode(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="strict")
    return value


def _strict_trade_date(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("trade_date must be strict YYYY-MM-DD text")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("trade_date must be strict YYYY-MM-DD text")
    return value


def _strict_symbol(value: Any) -> str:
    text = str(_decode(value)).strip()
    if not _SYMBOL_RE.fullmatch(text):
        raise ValueError("symbol must be a six-digit code")
    return text


def _strict_tag(value: Any) -> str:
    text = str(value).strip()
    if not _TAG_RE.fullmatch(text):
        raise ValueError("auction tag must be four digits")
    return text


def _optional_nonnegative_int(value: Any, *, field_name: str) -> int | None:
    value = _decode(value)
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a non-negative integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a non-negative integer") from exc
    if isinstance(value, float) and value != parsed:
        raise ValueError(f"{field_name} must be a non-negative integer")
    if isinstance(value, str) and str(parsed) != value.strip():
        raise ValueError(f"{field_name} must be a non-negative integer")
    if parsed < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return parsed


def _optional_price(value: Any) -> float | None:
    value = _decode(value)
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise ValueError("price_yuan must be finite")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("price_yuan must be finite") from exc
    if not math.isfinite(parsed):
        raise ValueError("price_yuan must be finite")
    return parsed


def _json_field(raw: Mapping[str, Any], key: str) -> Any:
    value = raw.get(key)
    if value in (None, ""):
        return None
    value = _decode(value)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a JSON string")
    return json.loads(value)


def _normalize_row(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError("top_amount row must be an object")
    symbol = _strict_symbol(raw.get("symbol") or raw.get("code"))
    amount = _optional_nonnegative_int(
        raw.get("auction_amount_yuan", raw.get("amount")),
        field_name="auction_amount_yuan",
    )
    bid = _optional_nonnegative_int(
        raw.get("bid_amount_yuan", raw.get("bid_amount")),
        field_name="bid_amount_yuan",
    )
    ask = _optional_nonnegative_int(
        raw.get("ask_amount_yuan", raw.get("ask_amount", raw.get("ar"))),
        field_name="ask_amount_yuan",
    )
    return {
        "symbol": symbol,
        "price_yuan": _optional_price(raw.get("price", raw.get("open_price"))),
        "change_ratio": normalize_auction_change_ratio(
            raw.get("change_pct", raw.get("open_pct"))
        ),
        "auction_amount_yuan": amount,
        "bid_amount_yuan": bid,
        "ask_amount_yuan": ask,
        "ask_amount_present": ask is not None,
    }


def _normalize_summary(raw: Any) -> Mapping[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError("auction summary must be an object")
    # Keep only writer-documented fields.  Unknown additions remain outside
    # the canonical business payload and cannot silently become facts.
    fields = (
        "ts",
        "tag",
        "total_stocks",
        "valid_stock_count",
        "unavailable_stock_count",
        "high_open_count",
        "low_open_count",
        "flat_open_count",
        "limit_up_count",
        "limit_down_count",
        "total_auction_amount_yuan",
        "total_limit_up_bid_amount_yuan",
    )
    return {
        name: _decode(raw[name])
        for name in fields
        if name in raw
    }


def _normalize_meta(raw: Any) -> Mapping[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError("auction meta must be an object")
    return {
        name: _decode(raw[name])
        for name in ("tag", "ts", "n")
        if name in raw
    }


@dataclass(frozen=True)
class RedisAuctionProjection:
    """One Redis auction projection tag, with explicit TopN scope."""

    trade_date: str
    tag: str
    key: str
    status: str
    scope: str
    rows: tuple[Mapping[str, Any], ...]
    summary: Mapping[str, Any]
    meta: Mapping[str, Any]
    requested_symbols: tuple[str, ...]
    missing_requested_symbols: tuple[str, ...]
    observed_at_ms: int
    evidence_ref: str
    content_hash: str = field(init=False)
    evidence_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _strict_trade_date(self.trade_date)
        _strict_tag(self.tag)
        if self.scope != REDIS_AUCTION_PROJECTION_SCOPE:
            raise ValueError("unsupported Redis auction projection scope")
        if isinstance(self.observed_at_ms, bool) or self.observed_at_ms <= 0:
            raise ValueError("observed_at_ms must be a positive epoch-ms integer")
        rows = tuple(deep_freeze(dict(row)) for row in self.rows)
        summary = deep_freeze(dict(self.summary))
        meta = deep_freeze(dict(self.meta))
        requested = tuple(sorted(set(self.requested_symbols)))
        missing = tuple(sorted(set(self.missing_requested_symbols)))
        object.__setattr__(self, "rows", rows)
        object.__setattr__(self, "summary", summary)
        object.__setattr__(self, "meta", meta)
        object.__setattr__(self, "requested_symbols", requested)
        object.__setattr__(self, "missing_requested_symbols", missing)
        semantic_payload = {
            "contract_version": REDIS_AUCTION_PROJECTION_CONTRACT_VERSION,
            "hash_contract_versions": {
                "semantic": SEMANTIC_HASH_CONTRACT_VERSION,
                "evidence": EVIDENCE_HASH_CONTRACT_VERSION,
            },
            "trade_date": self.trade_date,
            "tag": self.tag,
            "scope": self.scope,
            "status": self.status,
            "rows": rows,
            "summary": summary,
            "meta": meta,
        }
        evidence_payload = {
            "contract_version": REDIS_AUCTION_PROJECTION_CONTRACT_VERSION,
            "trade_date": self.trade_date,
            "tag": self.tag,
            "scope": self.scope,
            "key": self.key,
            "requested_symbols": requested,
            "missing_requested_symbols": missing,
            "observed_at_ms": self.observed_at_ms,
            "evidence_ref": self.evidence_ref,
        }
        object.__setattr__(self, "content_hash", semantic_hash(semantic_payload))
        object.__setattr__(self, "evidence_hash", evidence_hash(evidence_payload))

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def selected_rows(self) -> tuple[Mapping[str, Any], ...]:
        wanted = set(self.requested_symbols)
        if not wanted:
            return self.rows
        return tuple(row for row in self.rows if row.get("symbol") in wanted)

    def as_mapping(self) -> Mapping[str, Any]:
        rows = tuple(dict(row) for row in self.rows)
        selected_rows = tuple(dict(row) for row in self.selected_rows)
        return {
            "trade_date": self.trade_date,
            "tag": self.tag,
            "key": self.key,
            "status": self.status,
            "scope": self.scope,
            "row_count": self.row_count,
            "rows": rows,
            "selected_rows": selected_rows,
            "summary": dict(self.summary),
            "meta": dict(self.meta),
            "requested_symbols": self.requested_symbols,
            "missing_requested_symbols": self.missing_requested_symbols,
            "observed_at_ms": self.observed_at_ms,
            "evidence_ref": self.evidence_ref,
            "content_hash": self.content_hash,
            "evidence_hash": self.evidence_hash,
        }


def read_redis_auction_projection(
    client: RedisAuctionProjectionClient,
    *,
    trade_date: str,
    observed_at_ms: int,
    tags: Sequence[str] = DEFAULT_AUCTION_TAGS,
    symbols: Sequence[str] = (),
    evidence_ref_prefix: str = "redis://market-auction",
) -> tuple[RedisAuctionProjection, ...]:
    """Read Redis auction projections without fallback or side effects.

    ``top_amount`` is a bounded projection.  Missing keys are represented as
    ``MISSING`` snapshots; no previous or later tag is substituted, and no
    symbol absent from the projection is synthesized.
    """

    date_text = _strict_trade_date(trade_date)
    date_tag = date_text.replace("-", "")
    normalized_tags = tuple(dict.fromkeys(_strict_tag(tag) for tag in tags))
    if not normalized_tags:
        raise ValueError("at least one auction tag is required")
    requested = tuple(sorted({_strict_symbol(symbol) for symbol in symbols}))
    result: list[RedisAuctionProjection] = []
    for tag in normalized_tags:
        key = f"market:auction:{date_tag}:{tag}"
        raw = dict(client.hgetall(key) or {})
        try:
            meta_value = _json_field(raw, "meta")
            summary_value = _json_field(raw, "summary")
            top_value = _json_field(raw, "top_amount")
            meta = _normalize_meta(meta_value)
            summary = _normalize_summary(summary_value)
            if top_value is None:
                rows: tuple[Mapping[str, Any], ...] = ()
                status = "MISSING"
            elif not isinstance(top_value, list):
                raise ValueError("top_amount must be a JSON array")
            else:
                normalized_rows = tuple(_normalize_row(row) for row in top_value)
                symbols_seen = [str(row["symbol"]) for row in normalized_rows]
                if len(symbols_seen) != len(set(symbols_seen)):
                    raise ValueError("top_amount contains duplicate symbols")
                rows = tuple(sorted(normalized_rows, key=lambda row: str(row["symbol"])))
                status = "READY" if rows else "MISSING"
            available_symbols = {str(row["symbol"]) for row in rows}
            missing = tuple(symbol for symbol in requested if symbol not in available_symbols)
            result.append(
                RedisAuctionProjection(
                    trade_date=date_text,
                    tag=tag,
                    key=key,
                    status=status,
                    scope=REDIS_AUCTION_PROJECTION_SCOPE,
                    rows=rows,
                    summary=summary,
                    meta=meta,
                    requested_symbols=requested,
                    missing_requested_symbols=missing,
                    observed_at_ms=observed_at_ms,
                    evidence_ref=f"{evidence_ref_prefix.rstrip('/')}/{date_text}/{tag}",
                )
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            result.append(
                RedisAuctionProjection(
                    trade_date=date_text,
                    tag=tag,
                    key=key,
                    status="INVALID",
                    scope=REDIS_AUCTION_PROJECTION_SCOPE,
                    rows=(),
                    summary={},
                    meta={},
                    requested_symbols=requested,
                    missing_requested_symbols=requested,
                    observed_at_ms=observed_at_ms,
                    evidence_ref=f"{evidence_ref_prefix.rstrip('/')}/{date_text}/{tag}",
                )
            )
    return tuple(result)


__all__ = [
    "DEFAULT_AUCTION_TAGS",
    "REDIS_AUCTION_PROJECTION_CONTRACT_VERSION",
    "REDIS_AUCTION_PROJECTION_SCOPE",
    "RedisAuctionProjection",
    "RedisAuctionProjectionClient",
    "read_redis_auction_projection",
]
