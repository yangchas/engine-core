"""Read-only live input layer for the legacy Redis caches.

One place for the Redis shapes that the real probes used to re-implement:
hash caches (``cache:hot_plates``, ``cache:hot_rank``, ``cache:limit_truth``,
``cache:yest_limit_pool``, ``market:stock_plate``, ``config:plate_mapping:s2p``)
and the JSON anchor archive (``market:auction:anchor``).  The reader calls only
``type``/``hlen``/``hscan_iter``/``get``/``ttl`` on an injected client and never
writes, repairs or interprets rows; row semantics stay in the data functions.

``LiveInputCache.refresh`` keeps the last non-empty read when a later read is
empty or the scan was inconsistent: an empty response is "no information", not
"the data is gone" (the legacy writers refresh these keys once or twice a day).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

LIVE_INPUT_CONTRACT_VERSION = "LiveInputReadV1"

FRESH = "FRESH"
MISSING = "MISSING"
PRESERVED_PREVIOUS = "PRESERVED_PREVIOUS"
EXPIRED_PREVIOUS = "EXPIRED_PREVIOUS"

# Per-input bound on how long a preserved (last non-empty) read may be used
# as current.  Date-keyed caches are written once or twice a day, so the
# bounds are hours; the anchor archive is rewritten around the auction, so its
# bound is short.  Beyond the bound a read is EXPIRED_PREVIOUS (degraded).
DEFAULT_MAX_AGE_MS: Mapping[str, int] = {
    "hot_plates": 6 * 3600 * 1000,
    "hot_rank": 6 * 3600 * 1000,
    "limit_truth": 6 * 3600 * 1000,
    "yest_limit_pool": 6 * 3600 * 1000,
    "stock_plate": 24 * 3600 * 1000,
    "s2p": 24 * 3600 * 1000,
    "anchor_archive": 30 * 60 * 1000,
}

_META_KEYS = {
    "hot_plates": "cache:hot_plates_meta:{date}",
    "hot_rank": "cache:hot_rank_meta:{date}",
    "limit_truth": "cache:limit_truth_meta:{date}",
    "yest_limit_pool": "cache:yest_limit_pool_meta:{previous_date}",
}

_HASH_INPUTS = {
    "hot_plates": "cache:hot_plates:{date}",
    "hot_rank": "cache:hot_rank:{date}",
    "limit_truth": "cache:limit_truth:{date}",
    "yest_limit_pool": "cache:yest_limit_pool:{previous_date}",
    "stock_plate": "market:stock_plate",
    "s2p": "config:plate_mapping:s2p",
}


def _text(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


@dataclass(frozen=True)
class LiveInputRead:
    """One read of one key.  ``rows`` are raw (field, text) pairs, or empty."""

    name: str
    key: str
    kind: str  # "hash" | "json"
    redis_type: str
    rows: Tuple[Tuple[str, str], ...]
    payload: Any
    scan_consistent: bool
    ttl_seconds: Optional[int]
    content_sha256: str
    observed_at_ms: int
    hlen_before: Optional[int] = None
    hlen_after: Optional[int] = None
    meta_key: Optional[str] = None
    meta: Any = None  # parsed JSON, {"valid_json": False} when undecodable, None when absent

    @property
    def is_empty(self) -> bool:
        if self.kind == "hash":
            return len(self.rows) == 0
        return self.payload is None

    @property
    def usable(self) -> bool:
        return (not self.is_empty) and self.scan_consistent


@dataclass(frozen=True)
class LiveInputState:
    """The value a consumer should use plus why it is (or is not) current."""

    name: str
    status: str
    read: Optional[LiveInputRead]
    reason: Optional[str]
    last_refreshed_ms: Optional[int]
    last_attempt_ms: int

    def as_trace(self) -> Mapping[str, Any]:
        read = self.read
        return {
            "contract": LIVE_INPUT_CONTRACT_VERSION,
            "name": self.name,
            "status": self.status,
            "reason": self.reason,
            "key": read.key if read else None,
            "row_count": (len(read.rows) if read and read.kind == "hash" else None),
            "content_sha256": read.content_sha256 if read else None,
            "last_refreshed_ms": self.last_refreshed_ms,
            "last_attempt_ms": self.last_attempt_ms,
        }


class LiveInputReader:
    """Bounded read-only access to the legacy Redis caches."""

    def __init__(self, client: Any, *, now_ms: Callable[[], int]) -> None:
        self._client = client
        self._now_ms = now_ms

    def key_for(self, name: str, *, trade_date: str, previous_trade_date: Optional[str] = None) -> str:
        if name == "anchor_archive":
            return "market:auction:anchor:" + trade_date.replace("-", "")
        try:
            template = _HASH_INPUTS[name]
        except KeyError as exc:
            raise ValueError("unknown live input: %s" % name) from exc
        if "{previous_date}" in template and not previous_trade_date:
            raise ValueError("%s needs previous_trade_date" % name)
        return template.format(date=trade_date, previous_date=previous_trade_date)

    def read(self, name: str, *, trade_date: str, previous_trade_date: Optional[str] = None) -> LiveInputRead:
        key = self.key_for(name, trade_date=trade_date, previous_trade_date=previous_trade_date)
        if name == "anchor_archive":
            return self._read_json(name, key)
        meta_key = None
        if name in _META_KEYS:
            meta_key = _META_KEYS[name].format(date=trade_date, previous_date=previous_trade_date)
        return self._read_hash(name, key, meta_key)

    def _read_meta(self, meta_key: str) -> Any:
        raw = self._client.get(meta_key)
        if raw is None or raw == "" or raw == b"":
            return None
        try:
            return json.loads(_text(raw))
        except (TypeError, ValueError):
            return {"valid_json": False}

    def _read_hash(self, name: str, key: str, meta_key: Optional[str] = None) -> LiveInputRead:
        client = self._client
        redis_type = _text(client.type(key))
        rows: Tuple[Tuple[str, str], ...] = ()
        consistent = True
        before = after = None
        meta: Any = None
        if redis_type == "hash":
            before = int(client.hlen(key))
            items = sorted((_text(k), _text(v)) for k, v in client.hscan_iter(key))
            after = int(client.hlen(key))
            rows = tuple(items)
            consistent = before == after == len(items)
            if meta_key is not None:
                meta = self._read_meta(meta_key)
        digest = hashlib.sha256()
        for field, raw in rows:
            digest.update(field.encode() + b"\0" + raw.encode() + b"\n")
        return LiveInputRead(
            name=name, key=key, kind="hash", redis_type=redis_type, rows=rows,
            payload=None, scan_consistent=consistent, ttl_seconds=None,
            content_sha256=digest.hexdigest(), observed_at_ms=self._now_ms(),
            hlen_before=before, hlen_after=after, meta_key=meta_key, meta=meta,
        )

    def _read_json(self, name: str, key: str) -> LiveInputRead:
        client = self._client
        raw = client.get(key)
        ttl = client.ttl(key) if raw is not None else None
        payload = None
        consistent = True
        text = _text(raw) if raw is not None else ""
        if raw is not None:
            try:
                payload = json.loads(text)
            except ValueError:
                consistent = False
            else:
                consistent = isinstance(payload, (dict, list))
        return LiveInputRead(
            name=name, key=key, kind="json", redis_type="string" if raw is not None else "none",
            rows=(), payload=payload, scan_consistent=consistent,
            ttl_seconds=int(ttl) if isinstance(ttl, int) else None,
            content_sha256=hashlib.sha256(text.encode()).hexdigest(), observed_at_ms=self._now_ms(),
        )


class LiveInputCache:
    """Last-good store: an empty or inconsistent read never replaces data."""

    def __init__(self, max_age_ms: Optional[Mapping[str, int]] = None) -> None:
        # Per-input bound on how long a preserved read may be used as current.
        # Beyond it the state is EXPIRED_PREVIOUS: the data stays visible as
        # evidence but a consumer must treat it as degraded, not current.
        self._max_age_ms = dict(DEFAULT_MAX_AGE_MS)
        self._max_age_ms.update(max_age_ms or {})
        self._good: Dict[Tuple[str, str], Tuple[LiveInputRead, int]] = {}

    def refresh(self, read: LiveInputRead) -> LiveInputState:
        now = read.observed_at_ms
        identity = (read.name, read.key)
        if read.usable:
            self._good[identity] = (read, now)
            return LiveInputState(read.name, FRESH, read, None, now, now)
        reason = "scan_inconsistent" if (not read.scan_consistent and not read.is_empty) else (
            "invalid_json" if (read.kind == "json" and not read.scan_consistent) else "empty_response"
        )
        previous = self._good.get(identity)
        if previous is not None:
            limit = self._max_age_ms.get(read.name)
            expired = limit is not None and now - previous[1] > limit
            return LiveInputState(
                read.name, EXPIRED_PREVIOUS if expired else PRESERVED_PREVIOUS,
                previous[0], reason, previous[1], now,
            )
        return LiveInputState(read.name, MISSING, None, reason, None, now)

    def state(self, name: str) -> Optional[Tuple[LiveInputRead, int]]:
        matches = [value for (input_name, _), value in self._good.items() if input_name == name]
        return matches[-1] if matches else None


__all__ = [
    "DEFAULT_MAX_AGE_MS", "EXPIRED_PREVIOUS", "FRESH", "LIVE_INPUT_CONTRACT_VERSION", "LiveInputCache", "LiveInputRead",
    "LiveInputReader", "LiveInputState", "MISSING", "PRESERVED_PREVIOUS",
]
