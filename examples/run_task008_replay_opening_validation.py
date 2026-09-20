"""Replay a frozen real Q2 capture through the opening Engine shadow.

This runner consumes an already captured JSONL file.  It does not import a
Redis client and never contacts Redis, TDengine, RabbitMQ, or any effect
path.  The input capture remains the source evidence; this command only
rebuilds the Core projection at an explicit historical observation time and
compares ordered/shuffled input passes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from engine_core import (  # noqa: E402
    DataStatus,
    DeterministicEngine,
    EngineSignal,
    FreshnessPolicy,
    MarketStateReducer,
    OpeningShadowStrategy,
    Q2ProjectionSnapshot,
    SignalKind,
    WindowManager,
    WindowSpec,
    build_q2_projection,
    canonical_json,
)


def _parse_observed_at(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("observed-at must include a timezone")
    return parsed


def _load_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("capture row must be an object")
                rows.append(value)
    if not rows:
        raise ValueError("capture is empty")
    return rows


def _raw_hash(row: Mapping[str, Any]) -> dict[str, Any]:
    """Map the frozen Q2 capture dialect to the existing adapter dialect."""

    aliases = ("mk", "name", "px", "pc", "amt", "vol", "ts", "ph", "ls", "am", "br", "ar", "amt2m", "amt5m", "spd1m", "vec3m", "vec5m")
    return {name: row[name] for name in aliases if name in row}


def _projection(
    rows: list[dict[str, Any]],
    *,
    trade_date: str,
    observed_at: datetime,
    stale_after_ms: int,
) -> Q2ProjectionSnapshot:
    # Keep the symbol-to-payload association independent of input order.  The
    # shuffled pass is intended to test deterministic replay, not to permute
    # values between symbols by zipping a sorted key list with unsorted rows.
    raw = {
        str(row["symbol"]).zfill(6): _raw_hash(row)
        for row in rows
    }
    expected = tuple(sorted(raw))
    return build_q2_projection(
        trade_date,
        observed_at,
        expected,
        raw,
        freshness_policy=FreshnessPolicy(
            stale_after_ms=stale_after_ms,
            max_future_skew_ms=0,
        ),
        source_id="replay:production-ground-truth-q2",
    )


def _engine_result(
    projection: Q2ProjectionSnapshot,
    *,
    trade_date: str,
    symbol: str,
    logical_time_ms: int,
) -> dict[str, Any]:
    engine = DeterministicEngine(
        MarketStateReducer(),
        WindowManager((WindowSpec("opening", logical_time_ms, logical_time_ms + 1),)),
        OpeningShadowStrategy(scope_id=symbol),
        session_id=trade_date,
        phase="OPENING",
    )
    engine.submit(EngineSignal("replay-opening-market", logical_time_ms, 1, SignalKind.MARKET_UPDATE, projection))
    engine.submit(
        EngineSignal(
            "replay-opening-timer",
            logical_time_ms + 1,
            2,
            SignalKind.TIMER,
            {"trigger_id": "OPENING_0932", "close_windows": ("opening",)},
        )
    )
    result = engine.run_until_empty().strategy_results[-1]
    return {
        "symbol": symbol,
        "processed_signals": 2,
        "content_hash": result.content_hash,
        "state": result.state,
        "fact_status": result.trace.get("fact_status"),
        "decision_status": result.trace.get("decision_status"),
        "source_time_range": result.trace.get("source_time_range"),
        "opening_fact": result.trace.get("opening_fact"),
    }


def _run_pass(
    rows: list[dict[str, Any]],
    *,
    trade_date: str,
    observed_at: datetime,
    stale_after_ms: int,
    symbols: tuple[str, ...],
    shuffled: bool,
) -> dict[str, Any]:
    ordered = list(rows)
    if shuffled:
        random.Random("TASK-008-replay-opening").shuffle(ordered)
    projection = _projection(
        ordered,
        trade_date=trade_date,
        observed_at=observed_at,
        stale_after_ms=stale_after_ms,
    )
    logical_time_ms = int(observed_at.astimezone(timezone.utc).timestamp() * 1000)
    return {
        "input_order": "SHUFFLED" if shuffled else "ORDERED",
        "projection": {
            "status": projection.status.value,
            "consistency_status": projection.consistency_status,
            "coverage": projection.coverage,
            "expected_count": len(projection.expected_symbols),
            "observed_count": len(projection.quotes),
            "missing_count": len(projection.missing_symbols),
            "stale_count": len(projection.stale_symbols),
            "oldest_source_time_ms": projection.oldest_source_time_ms,
            "newest_source_time_ms": projection.newest_source_time_ms,
            "content_hash": projection.content_hash,
        },
        "engine": {
            symbol: _engine_result(
                projection,
                trade_date=trade_date,
                symbol=symbol,
                logical_time_ms=logical_time_ms,
            )
            for symbol in symbols
            if symbol in projection.expected_symbols
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--observed-at", required=True)
    parser.add_argument("--stale-after-ms", type=int, required=True)
    parser.add_argument("--symbols", default="000001,000002,000338,600519")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.stale_after_ms < 0:
        parser.error("stale-after-ms must be nonnegative")
    observed_at = _parse_observed_at(args.observed_at)
    rows = _load_rows(args.input)
    symbols = tuple(sorted({item.strip() for item in args.symbols.split(",") if item.strip()}))
    ordered = _run_pass(
        rows,
        trade_date=args.trade_date,
        observed_at=observed_at,
        stale_after_ms=args.stale_after_ms,
        symbols=symbols,
        shuffled=False,
    )
    shuffled = _run_pass(
        rows,
        trade_date=args.trade_date,
        observed_at=observed_at,
        stale_after_ms=args.stale_after_ms,
        symbols=symbols,
        shuffled=True,
    )
    comparison = {
        "projection_content_hash_equal": ordered["projection"]["content_hash"] == shuffled["projection"]["content_hash"],
        "engine_content_hash_equal": {
            symbol: ordered["engine"][symbol]["content_hash"] == shuffled["engine"][symbol]["content_hash"]
            for symbol in ordered["engine"]
        },
    }
    result = {
        "contract_version": "Task008ReplayOpeningValidationV1",
        "trade_date": args.trade_date,
        "observed_at": observed_at.isoformat(),
        "input": str(args.input),
        "input_sha256": hashlib.sha256(args.input.read_bytes()).hexdigest(),
        "row_count": len(rows),
        "symbols": symbols,
        "ordered": ordered,
        "shuffled": shuffled,
        "comparison": comparison,
        "deterministic": comparison["projection_content_hash_equal"] and all(comparison["engine_content_hash_equal"].values()),
        "normal_opening_pass": "UNPROVEN",
        "replay_status": "REPLAY_PARTIAL" if ordered["projection"]["status"] != DataStatus.READY.value else "REPLAY_READY_BOUNDED",
        "production_side_effects": "NONE_OBSERVED",
        "side_effect_boundary": "frozen capture + in-memory Q2 projection/Engine only",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(canonical_json(result) + "\n", encoding="utf-8")
    print(canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
