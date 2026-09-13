"""Probe the narrow, read-only engine_next auction snapshot loader.

This migration probe deliberately calls only the verified
``IntradayDataHub.load_auction_snapshots`` path from an exact engine_next
release.  It does not build ``IntradayContext``, invoke recovery/fallback
logic, access network connectors, write Redis/TDengine, consume RabbitMQ, or
run a strategy.  The purpose is to establish whether the old Redis snapshot
reader can be wrapped as a future core Provider without importing the old
context builder's side effects.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


def _strict_symbols(value: str) -> tuple[str, ...]:
    symbols = tuple(sorted({item.strip() for item in value.split(",") if item.strip()}))
    if not symbols or any(len(item) != 6 or not item.isdigit() for item in symbols):
        raise ValueError("symbols must be comma-separated six-digit codes")
    return symbols


def _strict_tags(value: str) -> tuple[str, ...]:
    tags = tuple(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))
    if not tags:
        raise ValueError("at least one auction tag is required")
    if any(len(item) != 4 or not item.isdigit() for item in tags):
        raise ValueError("tags must be comma-separated four-digit values")
    return tags


def _select_rows(rows: Iterable[Mapping[str, Any]], symbols: tuple[str, ...]) -> list[dict[str, Any]]:
    wanted = set(symbols)
    selected = [dict(row) for row in rows if str(row.get("symbol") or "") in wanted]
    return sorted(
        selected,
        key=lambda row: (str(row.get("tag") or ""), str(row.get("symbol") or "")),
    )


def _summarize(
    result: Any,
    *,
    trade_date: str,
    tags: tuple[str, ...],
    symbols: tuple[str, ...],
    writes: Sequence[str],
) -> dict[str, Any]:
    rows = list(getattr(result, "rows", ()) or ())
    selected = _select_rows(rows, symbols)
    counts: dict[str, int] = {tag: 0 for tag in tags}
    for row in rows:
        tag = str(row.get("tag") or "")
        if tag in counts:
            counts[tag] += 1
    # The legacy result field is unfortunately named ``redis_keys_written``
    # even though this read method populates it with keys it read. Preserve the
    # evidence without repeating that name as a new write claim.
    keys_reported = tuple(str(item) for item in (getattr(result, "redis_keys_written", ()) or ()))
    blocked_writes = tuple(str(item) for item in writes)
    return {
        "contract_version": "EngineNextAuctionLoaderProbeV1",
        "trade_date": trade_date,
        "tags": tags,
        "requested_symbols": symbols,
        "row_count": len(rows),
        "row_count_by_tag": counts,
        "selected_rows": selected,
        "source": str(getattr(result, "source", "") or ""),
        "notes": tuple(str(item) for item in (getattr(result, "notes", ()) or ())),
        "legacy_keys_reported": keys_reported,
        "guard_writes": blocked_writes,
        "read_only": not blocked_writes,
        "side_effect_boundary": (
            "blocked write attempts: " + ",".join(blocked_writes)
            if blocked_writes
            else "legacy Redis snapshot loader only; no context builder, network, TD, recovery, writer, Rabbit or effect"
        ),
    }


def _load_legacy(legacy_root: Path) -> Any:
    resolved = legacy_root.resolve()
    if not (resolved / "engine_next" / "runtime").is_dir():
        raise ValueError("legacy-root must contain engine_next/runtime")
    sys.path.insert(0, str(resolved))
    from engine_next.runtime.intraday_data_hub import (  # type: ignore[import-not-found]
        IntradayDataHub,
    )

    return IntradayDataHub


def probe(
    *,
    legacy_root: Path,
    trade_date: str,
    tags: tuple[str, ...],
    symbols: tuple[str, ...],
) -> dict[str, Any]:
    """Read the exact legacy Redis loader behind a fail-closed write guard."""

    import redis  # type: ignore[import-not-found]

    # Importing the guard from the context probe keeps the write command list
    # identical across both migration probes without importing any legacy code.
    from run_engine_next_context_probe import GuardRedis

    inner = redis.Redis(
        host=os.environ.get("REDIS_HOST", "127.0.0.1"),
        port=int(os.environ.get("REDIS_PORT", "6379")),
        db=int(os.environ.get("REDIS_DB", "0")),
        password=os.environ.get("REDIS_PASSWORD"),
        decode_responses=True,
        socket_timeout=5,
        socket_connect_timeout=5,
    )
    guarded = GuardRedis(inner)
    try:
        hub = _load_legacy(legacy_root)(redis_client=guarded)
        result = hub.load_auction_snapshots(trade_date, tags=tags)
        return _summarize(
            result,
            trade_date=trade_date,
            tags=tags,
            symbols=symbols,
            writes=guarded.writes,
        )
    finally:
        inner.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-root", type=Path, required=True)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--tags", default="0920,0924,0925")
    parser.add_argument("--symbols", default="600519,000001,000002")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    tags = _strict_tags(args.tags)
    symbols = _strict_symbols(args.symbols)
    result = probe(
        legacy_root=args.legacy_root,
        trade_date=args.trade_date,
        tags=tags,
        symbols=symbols,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(result, output, ensure_ascii=False, sort_keys=True, indent=2, default=str)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    return 0 if result["read_only"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
