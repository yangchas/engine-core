"""Injected read-only adapters for the legacy shared Q2 and TD outputs."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Mapping

from .contracts import QuoteCursor, QuoteRead, QuoteState
from .source_policy import ALLOWED_SOURCES, choose_source


def _text(value):
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _symbol(value):
    raw = _text(value).strip()
    if raw.isdigit() and len(raw) <= 6:
        return raw.zfill(6)
    raise ValueError("invalid equity symbol in q2 active set")


def _int(value):
    if value in (None, "", b""):
        return None
    try:
        return int(float(_text(value)))
    except (TypeError, ValueError, OverflowError):
        return None


def _state(symbol, raw, read_at, source):
    fields = {_text(k): _text(v) for k, v in (raw or {}).items()}
    stamp = _int(fields.get("ts"))
    try:
        event_at = datetime.fromtimestamp(stamp / 1000, timezone.utc) if stamp is not None else None
    except (OverflowError, OSError, ValueError):
        event_at = None
    quality = []
    if not fields:
        quality.append("HASH_MISSING")
    if event_at is None:
        quality.append("EVENT_TIME_MISSING")
    quality.append("AVAILABLE_AT_UNKNOWN")
    if source == "legacy_shared_q2":
        quality.append("LEGACY_SHARED_PROJECTION")
        if fields.get("vol") not in (None, ""):
            quality.append("VOLUME_UNIT_UNKNOWN")
    else:
        quality.append("LEGACY_TD")
    return QuoteState(
        symbol=symbol, ts=event_at, read_at=read_at, available_at=None, source=source,
        price_milli=_int(fields.get("px")), pre_close_milli=_int(fields.get("pc")),
        high_milli=_int(fields.get("mx")), low_milli=_int(fields.get("mn")),
        # Q2 has no open field; absence remains missing.
        amount_yuan=_int(fields.get("amt")), volume_source_units=_int(fields.get("vol")),
        a20_milli=_int(fields.get("a20")), a24_milli=_int(fields.get("a24")),
        a25_milli=_int(fields.get("a25")), limit_state=_int(fields.get("ls")),
        quality=tuple(quality), raw=fields,
    )


class SharedQuoteSource:
    """Read active Q2 hashes in one bounded pipeline, or an injected TD reader."""
    def __init__(self, *, redis: Any = None, trade_date: str, td_reader: Any = None,
                 clock=None, batch_size: int = 1000):
        self.redis, self.trade_date, self.td_reader = redis, trade_date, td_reader
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self.batch_size = batch_size

    def read_since(self, cursor: QuoteCursor | None = None, deadline: float | None = None,
                   *, source: str = "legacy_shared_q2") -> QuoteRead:
        if source not in ALLOWED_SOURCES:
            raise ValueError("unknown quote source")
        cursor = cursor or QuoteCursor()
        read_start = self.clock()
        if deadline is not None and time.monotonic() > deadline:
            raise TimeoutError("quote read deadline exceeded")
        if source == "legacy_shared_q2":
            if self.redis is None:
                raise RuntimeError("legacy_shared_q2 reader is not configured")
            rows, expected = self._read_q2(read_start)
        else:
            if self.td_reader is None:
                raise RuntimeError("legacy_td reader is not configured")
            provider = getattr(self.td_reader, "read_since", None)
            if not callable(provider):
                raise TypeError("legacy_td adapter must implement read_since(cursor, deadline)")
            raw_rows = provider(cursor, deadline)
            rows = tuple(self._from_td(row, read_start) for row in raw_rows)
            expected = None
        read_end = self.clock()
        next_ts = max((int(row.ts.timestamp() * 1000) for row in rows if row.ts), default=cursor.event_time_ms)
        next_cursor = QuoteCursor(round_id=cursor.round_id + 1, event_time_ms=next_ts)
        missing = sum("HASH_MISSING" in row.quality for row in rows)
        return QuoteRead(rows, next_cursor, read_start, read_end, expected, missing,
                         bool(missing), source, "UNKNOWN")

    def _read_q2(self, read_at):
        members = self.redis.smembers(f"q2:active:{self.trade_date.replace('-', '')}")
        symbols = sorted({_symbol(item) for item in members})
        rows = []
        # The active-set + HGETALL reads are not atomic; coverage and missing hashes stay visible.
        for offset in range(0, len(symbols), self.batch_size):
            batch = symbols[offset:offset + self.batch_size]
            pipe = self.redis.pipeline(transaction=False)
            for symbol in batch:
                pipe.hgetall(f"q2:{symbol}")
            replies = pipe.execute()
            rows.extend(_state(symbol, raw, read_at, "legacy_shared_q2")
                        for symbol, raw in zip(batch, replies))
            if len(replies) != len(batch):
                rows.extend(_state(symbol, {}, read_at, "legacy_shared_q2")
                            for symbol in batch[len(replies):])
        return tuple(rows), len(symbols)

    @staticmethod
    def _from_td(raw: Mapping[str, Any], read_at: datetime) -> QuoteState:
        symbol = _symbol(raw.get("symbol"))
        stamp = raw.get("ts")
        if isinstance(stamp, datetime):
            event_at = stamp.astimezone(timezone.utc) if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
        elif stamp is not None:
            try:
                numeric = int(stamp)
                event_at = datetime.fromtimestamp(numeric / (1000 if numeric > 10**11 else 1), timezone.utc)
            except (ValueError, TypeError, OverflowError, OSError):
                event_at = None
        else:
            event_at = None
        quality = ["AVAILABLE_AT_UNKNOWN", "LEGACY_TD"]
        if event_at is None: quality.append("EVENT_TIME_MISSING")
        return QuoteState(symbol, event_at, read_at, None, "legacy_td",
                          price_milli=_int(raw.get("px_milli")),
                          pre_close_milli=_int(raw.get("pc_milli")),
                          high_milli=_int(raw.get("high_milli")), low_milli=_int(raw.get("low_milli")),
                          open_milli=_int(raw.get("open_milli")), amount_yuan=_int(raw.get("amt_yuan")),
                          volume_source_units=_int(raw.get("vol_source_units")),
                          a20_milli=_int(raw.get("a20_milli")), a24_milli=_int(raw.get("a24_milli")),
                          a25_milli=_int(raw.get("a25_milli")), limit_state=_int(raw.get("limit_state")),
                          quality=tuple(quality), raw=dict(raw))


class QuoteSource:
    """Allow-list wrapper enforcing the caller's explicit source choice."""
    def __init__(self, sources):
        self.sources = dict(sources)
        if not self.sources or not self.sources.keys() <= ALLOWED_SOURCES:
            raise ValueError("sources contain an unsupported source")

    def read_since(self, cursor=None, deadline=None, *, source=None):
        reader = choose_source(self.sources, source)
        return reader.read_since(cursor, deadline, source=source or next(iter(self.sources)))

