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
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from engine_core import (  # noqa: E402
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


def _truncate_to_second(value: datetime) -> datetime:
    """Apply the replay contract's whole-second event-time precision."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("time values must include a timezone")
    return value.replace(microsecond=0)


def _canonical_quote_to_row(symbol: str, quote: Mapping[str, Any]) -> dict[str, Any]:
    """Project a frozen Redis Q2 capture quote back to adapter raw fields."""

    aliases = {
        "market": "mk",
        "name": "name",
        "price_milli": "px",
        "pre_close_milli": "pc",
        "amount_yuan": "amt",
        "volume_lots": "vol",
        "source_record_time_ms": "ts",
        "phase": "ph",
        "limit_state": "ls",
        "auction_amount_yuan": "am",
        "auction_bid_amount_yuan": "br",
        "auction_ask_amount_yuan": "ar",
        "amount_2m_yuan": "amt2m",
        "amount_5m_yuan": "amt5m",
        "speed_1m_bp": "spd1m",
        "vector_3m_bp": "vec3m",
        "vector_5m_bp": "vec5m",
    }
    row: dict[str, Any] = {"symbol": symbol}
    for canonical_name, raw_name in aliases.items():
        if canonical_name in quote and quote[canonical_name] is not None:
            row[raw_name] = quote[canonical_name]
    return row


def _load_rows_from_bytes(content: bytes, *, suffix: str) -> list[dict[str, Any]]:
    """Decode the exact bytes whose digest is recorded in the replay report."""

    text = content.decode("utf-8")
    stripped = text.lstrip()
    if stripped.startswith("{") and suffix.lower() == ".json":
        payload = json.loads(text)
        quotes = payload.get("projection", {}).get("quotes")
        if isinstance(quotes, dict):
            return [
                _canonical_quote_to_row(str(symbol), quote)
                for symbol, quote in sorted(quotes.items())
                if isinstance(quote, dict)
            ]
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("capture row must be an object")
            rows.append(value)
    if not rows:
        raise ValueError("capture is empty")
    return rows


def _load_rows(path: Path) -> list[dict[str, Any]]:
    """Load JSONL raw rows or a frozen Redis Q2 projection capture."""

    return _load_rows_from_bytes(path.read_bytes(), suffix=path.suffix)


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
    expected_symbols: Sequence[str] | None = None,
) -> Q2ProjectionSnapshot:
    # Keep the symbol-to-payload association independent of input order.  The
    # shuffled pass is intended to test deterministic replay, not to permute
    # values between symbols by zipping a sorted key list with unsorted rows.
    normalized_symbols = [str(row["symbol"]).zfill(6) for row in rows]
    if len(normalized_symbols) != len(set(normalized_symbols)):
        raise ValueError("duplicate symbol in Q2 capture after symbol normalization")
    raw = {
        symbol: _raw_hash(row)
        for symbol, row in zip(normalized_symbols, rows)
    }
    expected = tuple(
        sorted(
            raw
            if expected_symbols is None
            else {str(symbol).zfill(6) for symbol in expected_symbols}
        )
    )
    observed_at = _truncate_to_second(observed_at)
    return build_q2_projection(
        trade_date,
        observed_at,
        expected,
        raw,
        freshness_policy=FreshnessPolicy(
            stale_after_ms=stale_after_ms,
            # Timestamp subseconds are intentionally discarded for replay.
            # With an integer-second observation boundary, this admits only
            # the remainder of that same second, never the following second.
            max_future_skew_ms=999,
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
    observed_at = _truncate_to_second(observed_at)
    ordered = list(rows)
    if shuffled:
        random.Random("TASK-008-replay-opening").shuffle(ordered)
    projection = _projection(
        ordered,
        trade_date=trade_date,
        observed_at=observed_at,
        stale_after_ms=stale_after_ms,
    )
    # A current Redis Q2 capture is not a historical availability log.  At a
    # historical cutoff, never let a quote from a later event time (or a
    # different trade date / unknown source time) enter the Engine.  Preserve
    # the unfiltered projection above for evidence, and replay only the
    # event-time-eligible subset.  This does not prove historical availability.
    event_time_eligible_symbols = {
        symbol
        for symbol, quote in projection.quotes.items()
        if quote.source_record_time_ms is not None
        and "future_ts" not in quote.field_errors
        and "trade_date" not in quote.field_errors
    }
    normalized_rows = {
        str(row["symbol"]).zfill(6): row
        for row in rows
    }
    event_time_eligible_rows = [
        normalized_rows[symbol]
        for symbol in sorted(event_time_eligible_symbols)
        if symbol in normalized_rows
    ]
    engine_projection = _projection(
        event_time_eligible_rows,
        trade_date=trade_date,
        observed_at=observed_at,
        stale_after_ms=stale_after_ms,
        expected_symbols=projection.expected_symbols,
    )
    future_count = sum(
        "future_ts" in quote.field_errors for quote in projection.quotes.values()
    )
    trade_date_mismatch_count = sum(
        "trade_date" in quote.field_errors for quote in projection.quotes.values()
    )
    missing_source_time_count = sum(
        quote.source_record_time_ms is None for quote in projection.quotes.values()
    )
    sample_quote_count = sum(
        symbol in engine_projection.quotes for symbol in symbols
    )
    field_error_counts = Counter(
        error
        for quote in projection.quotes.values()
        for error in quote.field_errors
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
            "field_error_counts": dict(sorted(field_error_counts.items())),
            "oldest_source_time_ms": projection.oldest_source_time_ms,
            "newest_source_time_ms": projection.newest_source_time_ms,
            "content_hash": projection.content_hash,
        },
        "engine_input": {
            "eligible_quote_count": len(engine_projection.quotes),
            "expected_count": len(engine_projection.expected_symbols),
            "excluded_future_count": future_count,
            "excluded_trade_date_mismatch_count": trade_date_mismatch_count,
            "excluded_missing_source_time_count": missing_source_time_count,
            "sample_quote_count": sample_quote_count,
            "sample_quote_symbols": sorted(
                set(symbols) & set(engine_projection.quotes)
            ),
            "quality_status": (
                "NO_EVENT_TIME_ELIGIBLE_QUOTES"
                if not engine_projection.quotes
                else "PARTIAL_EVENT_TIME_ELIGIBLE_QUOTES"
                if len(engine_projection.quotes) < len(engine_projection.expected_symbols)
                else "EVENT_TIME_ELIGIBLE_QUOTES"
            ),
            "historical_available_at_status": "UNKNOWN",
            "filter_policy": (
                "same trade date and source event time <= observed_at; "
                "availability unknown"
            ),
        },
        "engine": {
            symbol: _engine_result(
                engine_projection,
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
    requested_observed_at = _parse_observed_at(args.observed_at)
    observed_at = _truncate_to_second(requested_observed_at)
    input_bytes = args.input.read_bytes()
    input_sha256 = hashlib.sha256(input_bytes).hexdigest()
    rows = _load_rows_from_bytes(input_bytes, suffix=args.input.suffix)
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
    ordered_engine_symbols = set(ordered["engine"])
    shuffled_engine_symbols = set(shuffled["engine"])
    engine_symbols = tuple(sorted(ordered_engine_symbols & shuffled_engine_symbols))
    projection_determinism_passed = (
        ordered["projection"]["content_hash"]
        == shuffled["projection"]["content_hash"]
    )
    ordered_sample_symbols = set(ordered["engine_input"]["sample_quote_symbols"])
    shuffled_sample_symbols = set(shuffled["engine_input"]["sample_quote_symbols"])
    engine_comparison_possible = bool(engine_symbols) and (
        ordered_engine_symbols == shuffled_engine_symbols
    ) and bool(ordered_sample_symbols) and (
        ordered_sample_symbols == shuffled_sample_symbols
    )
    comparison = {
        "projection_content_hash_equal": projection_determinism_passed,
        "engine_content_hash_equal": {
            symbol: ordered["engine"][symbol]["content_hash"]
            == shuffled["engine"][symbol]["content_hash"]
            for symbol in engine_symbols
        },
    }
    engine_comparison_passed = engine_comparison_possible and all(
        comparison["engine_content_hash_equal"].values()
    )
    deterministic = (
        engine_comparison_passed
        and projection_determinism_passed
    )
    if not projection_determinism_passed or (
        engine_comparison_possible and not engine_comparison_passed
    ):
        exit_code = 2
    elif not engine_comparison_possible:
        exit_code = 3
    else:
        exit_code = 0
    result = {
        "contract_version": "Task008ReplayOpeningValidationV4",
        "trade_date": args.trade_date,
        "observed_at": observed_at.isoformat(),
        "requested_observed_at": requested_observed_at.isoformat(),
        "time_precision_policy": "TRUNCATE_TO_WHOLE_SECONDS",
        "input": str(args.input),
        "input_sha256": input_sha256,
        "row_count": len(rows),
        "unique_symbol_count": len({str(row["symbol"]).zfill(6) for row in rows}),
        "symbols": symbols,
        "ordered": ordered,
        "shuffled": shuffled,
        "comparison": comparison,
        "deterministic": deterministic,
        # Execution, determinism, and input quality are independent outcomes.
        # Reaching report generation means both ordered and shuffled passes
        # completed; stale/missing/invalid facts are reported separately.
        "replay_execution_status": "COMPLETE",
        "replay_determinism_status": (
            "MISMATCH"
            if exit_code == 2
            else "NOT_COMPARABLE"
            if exit_code == 3
            else "PASS"
        ),
        "projection_determinism_status": (
            "PASS" if projection_determinism_passed else "MISMATCH"
        ),
        "engine_comparison_status": (
            "NOT_COMPARABLE"
            if not engine_comparison_possible
            else "PASS"
            if engine_comparison_passed
            else "MISMATCH"
        ),
        "engine_comparison_symbols": list(engine_symbols),
        "engine_sample_quote_symbols": sorted(ordered_sample_symbols),
        "projection_quality_status": ordered["projection"]["status"],
        "engine_input_quality_status": ordered["engine_input"]["quality_status"],
        "historical_available_at_status": "UNKNOWN",
        "exit_code": exit_code,
        "normal_opening_pass": "UNPROVEN",
        "production_side_effects": "NONE_OBSERVED",
        "side_effect_boundary": "frozen capture + in-memory Q2 projection/Engine only",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(canonical_json(result) + "\n", encoding="utf-8")
    print(canonical_json(result))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
