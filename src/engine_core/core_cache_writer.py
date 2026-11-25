"""Guarded writer for Core-owned isolated cache datasets.

This module defines the write boundary only; it does not create or connect a
Redis client.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Callable


def _check_key(key: Any) -> str:
    if not isinstance(key, str) or not key.startswith("core:cache:"):
        raise ValueError("cache key must use the core:cache: prefix")
    return key


class _ClientWhitelist:
    """Expose only the small, prefix-guarded client surface used here."""

    _ALLOWED = frozenset({"hset", "set", "expire", "rename", "delete"})

    def __init__(self, client: Any) -> None:
        self._client = client

    def __getattr__(self, method_name: str) -> Callable[..., Any]:
        if method_name not in self._ALLOWED:
            raise RuntimeError("client method is not allowed: " + method_name)
        method = getattr(self._client, method_name)

        def guarded(*args: Any, **kwargs: Any) -> Any:
            if method_name in {"hset", "set", "expire"}:
                key = args[0] if args else kwargs.get("name", kwargs.get("key"))
                _check_key(key)
            elif method_name == "rename":
                source = args[0] if args else kwargs.get("src")
                target = args[1] if len(args) > 1 else kwargs.get("dst")
                _check_key(source)
                _check_key(target)
            else:  # delete accepts one or more keys
                keys = args or tuple(kwargs.values())
                if not keys:
                    raise ValueError("delete requires at least one key")
                for key in keys:
                    _check_key(key)
            return method(*args, **kwargs)

        return guarded


def _decoded_rows(rows: Mapping[str, Any]) -> dict[str, Any]:
    decoded: dict[str, Any] = {}
    for symbol in sorted(rows, key=str):
        value = rows[symbol]
        if isinstance(value, str):
            try:
                decoded[str(symbol)] = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                decoded[str(symbol)] = value
        else:
            decoded[str(symbol)] = value
    return decoded


def _payload_sha256(rows: Mapping[str, Any]) -> str:
    payload = json.dumps(
        _decoded_rows(rows),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class CoreCacheWriter:
    """Write a complete hash dataset under the isolated Core cache prefix."""

    KEY_PREFIX = "core:cache:"
    META_PREFIX = "core:cache:meta:"

    def __init__(self, client: Any, *, now_ms: int) -> None:
        self.client = _ClientWhitelist(client)
        self._now_ms = now_ms

    def write_hash_dataset(
        self,
        dataset: str,
        trade_date: str,
        rows: Mapping[str, str | Mapping[str, Any]],
        *,
        source: str,
        field_units: Mapping[str, str],
        schema_version: str,
        ttl_seconds: int = 3 * 86400,
    ) -> dict[str, Any]:
        if not isinstance(rows, Mapping):
            raise TypeError("rows must be a mapping")
        if not rows:
            return {"written": False, "reason": "EMPTY_ROWS_PRESERVES_PREVIOUS"}

        target = _check_key(f"{self.KEY_PREFIX}{dataset}:{trade_date}")
        temporary = _check_key(f"{self.KEY_PREFIX}tmp:{dataset}:{trade_date}:{self._now_ms}")
        meta_key = _check_key(f"{self.META_PREFIX}{dataset}:{trade_date}")
        serialized: dict[str, str] = {}
        for symbol, value in rows.items():
            if isinstance(value, Mapping):
                serialized[str(symbol)] = json.dumps(
                    value, sort_keys=True, ensure_ascii=False
                )
            elif isinstance(value, str):
                serialized[str(symbol)] = value
            else:
                raise TypeError("each row must be a string or mapping")

        meta = {
            "schema_version": schema_version,
            "source": source,
            "row_count": len(serialized),
            "payload_sha256": _payload_sha256(rows),
            "available_at_ms": self._now_ms,
            "field_units": dict(field_units),
            "written_by": "engine_core.core_cache_writer",
            "contract": "CoreCacheMetaV1",
        }

        self.client.hset(temporary, mapping=serialized)
        self.client.expire(temporary, ttl_seconds)
        self.client.rename(temporary, target)
        self.client.expire(target, ttl_seconds)
        self.client.set(
            meta_key,
            json.dumps(meta, sort_keys=True, ensure_ascii=False, separators=(",", ":")),
            ex=ttl_seconds,
        )
        return {"written": True, "key": target, "meta_key": meta_key, "meta": meta}


def verify_core_cache(read_hash_rows: Mapping[str, Any], meta: Mapping[str, Any]) -> dict[str, Any]:
    """Check that cached payload and its availability metadata agree."""

    reasons: list[str] = []
    if not isinstance(read_hash_rows, Mapping):
        return {"valid": False, "reasons": ["rows_invalid"]}
    if not isinstance(meta, Mapping):
        return {"valid": False, "reasons": ["meta_invalid"]}
    if meta.get("row_count") != len(read_hash_rows):
        reasons.append("row_count_mismatch")
    if meta.get("payload_sha256") != _payload_sha256(read_hash_rows):
        reasons.append("payload_sha256_mismatch")
    available_at_ms = meta.get("available_at_ms")
    if isinstance(available_at_ms, bool) or not isinstance(available_at_ms, int) or available_at_ms <= 0:
        reasons.append("available_at_ms_invalid")
    return {"valid": not reasons, "reasons": reasons}
