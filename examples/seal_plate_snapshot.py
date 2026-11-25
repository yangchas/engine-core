"""Seal the legacy plate related Redis hashes into a dated, read-only snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from engine_core.live_inputs import LiveInputCache, LiveInputReader  # noqa: E402


_ALLOWED_METHODS = frozenset({"type", "hlen", "hscan_iter", "get", "ttl"})
_LIVE_INPUT_FILES = {
    "stock_plate": "stock_plate.json",
    "s2p": "s2p.json",
    "yest_limit_pool": "yest_limit_pool.json",
    "hot_plates": "hot_plates.json",
}


class ReadOnlyRedis:
    """Expose only methods needed by LiveInputReader and the stock_reason read."""

    def __init__(self, client: Any) -> None:
        self.__client = client

    def __getattr__(self, name: str) -> Any:
        if name not in _ALLOWED_METHODS:
            raise AttributeError("Redis method is not allowed: " + name)
        method = getattr(self.__client, name)
        if not callable(method):
            raise AttributeError("Redis method is not callable: " + name)
        return method


def _text(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _write_json(path: Path, value: Any) -> str:
    data = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    with path.open("xb") as handle:
        handle.write(data)
    return hashlib.sha256(data).hexdigest()


def _read_stock_reason(client: ReadOnlyRedis, now_ms: Callable[[], int]) -> tuple[list[dict[str, str]], dict[str, Any]]:
    key = "market:stock_reason"
    redis_type = _text(client.type(key))
    rows: list[tuple[str, str]] = []
    before = after = None
    consistent = True
    if redis_type == "hash":
        before = int(client.hlen(key))
        rows = sorted((_text(field), _text(value)) for field, value in client.hscan_iter(key))
        after = int(client.hlen(key))
        consistent = before == after == len(rows)
    observed = now_ms()
    return ([{"field": field, "value": value} for field, value in rows], {
        "name": "stock_reason", "key": key, "redis_type": redis_type,
        "observed_at_ms": observed,
        "observed_at": datetime.fromtimestamp(observed / 1000, timezone.utc).isoformat(),
        "scan_consistent": consistent, "hlen_before": before, "hlen_after": after,
        "meta": None,
    })


def seal_plate_snapshot(
    client: Any, *, trade_date: str, previous_trade_date: str, out_root: Path | str,
    now_ms: Callable[[], int] | None = None,
) -> Path:
    """Read five hashes and write one immutable snapshot directory."""
    clock = now_ms or (lambda: int(time.time() * 1000))
    readonly = ReadOnlyRedis(client)
    reader = LiveInputReader(readonly, now_ms=clock)
    cache = LiveInputCache()
    output_data: dict[str, Any] = {}
    file_meta: dict[str, dict[str, Any]] = {}

    for name, filename in _LIVE_INPUT_FILES.items():
        read = reader.read(name, trade_date=trade_date, previous_trade_date=previous_trade_date)
        state = cache.refresh(read)
        invalid_entries = 0
        if name == "s2p":
            decoded: dict[str, Any] = {}
            for field, raw in read.rows:
                try:
                    value = json.loads(raw)
                except (TypeError, ValueError):
                    value = raw
                    invalid_entries += 1
                else:
                    if not isinstance(value, list):
                        invalid_entries += 1
                        value = raw
                decoded[field] = value
            output_data[filename] = decoded
            row_count = len(decoded)
        elif name == "stock_plate":
            output_data[filename] = {field: raw for field, raw in read.rows}
            row_count = len(read.rows)
        else:
            output_data[filename] = [{"field": field, "value": raw} for field, raw in read.rows]
            row_count = len(read.rows)
        details: dict[str, Any] = {
            "input": name, "key": read.key, "status": state.status,
            "redis_type": read.redis_type, "observed_at_ms": read.observed_at_ms,
            "observed_at": datetime.fromtimestamp(read.observed_at_ms / 1000, timezone.utc).isoformat(),
            "scan_consistent": read.scan_consistent, "row_count": row_count,
            "content_sha256": read.content_sha256, "meta_key": read.meta_key,
            "meta": read.meta,
        }
        if name == "s2p":
            details["invalid_entries"] = invalid_entries
        file_meta[filename] = details

    stock_reason, stock_reason_meta = _read_stock_reason(readonly, clock)
    output_data["stock_reason.json"] = stock_reason
    file_meta["stock_reason.json"] = {**stock_reason_meta, "row_count": len(stock_reason)}

    target = Path(out_root) / trade_date
    target.parent.mkdir(parents=True, exist_ok=True)
    target.mkdir(exist_ok=False)
    files: dict[str, dict[str, Any]] = {}
    for filename, payload in output_data.items():
        digest = _write_json(target / filename, payload)
        files[filename] = {**file_meta[filename], "sha256": digest}
    manifest = {
        "contract": "PlateSnapshotV1", "trade_date": trade_date,
        "previous_trade_date": previous_trade_date,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
        "side_effect_boundary": "Redis TYPE/HLEN/HSCAN/GET/TTL only; no Redis or TD writes",
    }
    _write_json(target / "manifest.json", manifest)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--previous-trade-date", required=True)
    parser.add_argument("--out-root", type=Path, required=True)
    args = parser.parse_args()
    import redis  # type: ignore[import-not-found]

    client = redis.Redis(host=os.environ.get("REDIS_HOST", "127.0.0.1"),
                         port=int(os.environ.get("REDIS_PORT", "6379")),
                         db=int(os.environ.get("REDIS_DB", "0")),
                         password=os.environ.get("REDIS_PASSWORD"),
                         socket_timeout=10, socket_connect_timeout=5)
    try:
        output = seal_plate_snapshot(client, trade_date=args.trade_date,
                                     previous_trade_date=args.previous_trade_date,
                                     out_root=args.out_root)
    finally:
        client.close()
    print("sealed", output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
