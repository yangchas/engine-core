"""Bind real Redis auction/theme facts to one fact-only Core Engine session.

This is a post-market integration diagnostic. It reads one frozen Redis
0920/0924/0925 projection cohort plus the existing theme maps, submits the
real per-symbol projections to one ``DeterministicEngine``, and binds the
real theme result through the normal ``DATA_READY`` contract. It does not
claim those values were available at the historical auction cutoffs.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine_core import (  # noqa: E402
    AuctionShadowStrategy,
    DataRequest,
    DataResult,
    DataStatus,
    DeterministicEngine,
    EngineSignal,
    FrozenDataBundle,
    MarketStateReducer,
    SignalKind,
    TemporalDataGuard,
    WindowManager,
    WindowSpec,
    build_legacy_theme_delta_shadow_trace,
    canonical_json,
    read_redis_auction_projection,
    semantic_hash,
)
from engine_core.contracts import Provenance  # noqa: E402

try:  # Script execution resolves sibling examples directly.
    from run_real_redis_auction_engine_shadow import (  # type: ignore[import-not-found]  # noqa: E402
        ANCHOR_ORDER,
        build_engine_projection,
    )
    from run_real_theme_delta_strategy_shadow import (  # type: ignore[import-not-found]  # noqa: E402
        build_real_theme_delta_strategy_shadow_from_projections,
    )
except ImportError:  # Pytest/import execution resolves the package.
    from examples.run_real_redis_auction_engine_shadow import (  # noqa: E402
        ANCHOR_ORDER,
        build_engine_projection,
    )
    from examples.run_real_theme_delta_strategy_shadow import (  # noqa: E402
        build_real_theme_delta_strategy_shadow_from_projections,
    )


SHANGHAI = ZoneInfo("Asia/Shanghai")
THEME_DELTA_FUNCTION_ID = "theme_auction_delta_compat"
ANCHOR_CLOCKS = {
    "0920": time(9, 20),
    "0924": time(9, 24),
    "0925": time(9, 25),
}


class ReadOnlyRedis:
    """Narrow Redis facade that permits and records HGETALL only."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self.commands: list[dict[str, str]] = []

    def hgetall(self, key: str) -> Any:
        self.commands.append({"command": "HGETALL", "key": str(key)})
        return self._client.hgetall(key)

    def __getattr__(self, name: str) -> Any:
        raise PermissionError("real Engine shadow permits Redis HGETALL only")


def _strict_trade_date(value: str) -> str:
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("trade_date must use strict YYYY-MM-DD")
    return value


def _strict_symbol(value: str) -> str:
    if not isinstance(value, str) or len(value) != 6 or not value.isdigit():
        raise ValueError("symbol must be a six-digit code")
    return value


def _business_anchor_ms(trade_date: str, tag: str) -> int:
    local = datetime.combine(
        date.fromisoformat(trade_date), ANCHOR_CLOCKS[tag], tzinfo=SHANGHAI
    )
    return int(local.astimezone(timezone.utc).timestamp() * 1000)


def _choose_symbol(
    projections: Sequence[Any],
    *,
    requested_symbol: str | None,
) -> tuple[str, str]:
    if requested_symbol is not None:
        return _strict_symbol(requested_symbol), "EXPLICIT_OPERATOR_SYMBOL"
    by_tag = {projection.tag: projection for projection in projections}
    sets = [
        {str(row["symbol"]) for row in by_tag[tag].rows}
        for tag in ANCHOR_ORDER
    ]
    common = set.intersection(*sets) if sets else set()
    candidates = common or set.union(*sets)
    if not candidates:
        raise ValueError("no real symbols exist in the three Redis auction projections")
    basis = (
        "COMMON_SYMBOL_ACROSS_0920_0924_0925_TOP_AMOUNT"
        if common
        else "UNION_FALLBACK_NO_COMMON_SYMBOL"
    )
    return min(candidates), basis


def _theme_data_result(
    theme_shadow: Mapping[str, Any],
    *,
    trade_date: str,
    knowledge_as_of_ms: int,
) -> DataResult:
    """Represent the real Redis result as a live-acquisition Core input.

    ``available_at_ms`` deliberately remains unknown. The live-fetch marker
    only proves this process completed its Redis reads before the diagnostic
    Engine evaluation; it is not historical cutoff evidence.
    """

    observed_at_ms = int(theme_shadow["observed_at_ms"])
    if isinstance(observed_at_ms, bool) or not isinstance(observed_at_ms, int) or observed_at_ms <= 0:
        raise ValueError("theme observed_at_ms must be a positive epoch-millisecond integer")
    if isinstance(knowledge_as_of_ms, bool) or not isinstance(knowledge_as_of_ms, int) or knowledge_as_of_ms <= 0:
        raise ValueError("knowledge_as_of_ms must be a positive epoch-millisecond integer")
    facts = tuple(theme_shadow.get("facts", ()))
    is_available = theme_shadow.get("status") == "OBSERVED" and bool(facts)
    projection_hashes = theme_shadow.get("projection_content_hashes", {})
    provenance = tuple(
        Provenance(
            source_id=f"redis_market_auction_{tag}",
            source_kind="REDIS_PROJECTION",
            source_schema="RedisAuctionProjectionV1",
            source_trade_date=trade_date,
            effective_at_ms=None,
            observed_at_ms=observed_at_ms,
            evidence_ref=f"redis://market-auction/{trade_date}/{tag}",
            notes=(
                "scope=TOP_AMOUNT",
                "historical_available_at=UNKNOWN",
                "content_hash=" + str(projection_hashes.get(tag, "UNKNOWN")),
            ),
        )
        for tag in ("0924", "0925")
    ) + (
        Provenance(
            source_id="redis_theme_mapping",
            source_kind="REDIS_HASH_VIEW",
            source_schema="market:stock_plate+config:plate_mapping:s2p",
            source_trade_date=trade_date,
            effective_at_ms=None,
            observed_at_ms=observed_at_ms,
            evidence_ref="redis://market-theme-mapping/" + trade_date,
            notes=("historical_available_at=UNKNOWN",),
        ),
    )
    data = {
        "facts": facts,
        "scope": theme_shadow.get("scope", "REDIS_TOP_AMOUNT_INTERSECTION"),
        "projection_content_hashes": projection_hashes,
        "projection_evidence_hashes": theme_shadow.get(
            "projection_evidence_hashes", {}
        ),
        "row_count": theme_shadow.get("row_count", 0),
        "mapping_count": theme_shadow.get("mapping_count", 0),
        "mapping_missing_count": theme_shadow.get("mapping_missing_count", 0),
    }
    result = DataResult(
        request_id="redis-theme-delta:" + trade_date + ":" + semantic_hash(data),
        function_id=THEME_DELTA_FUNCTION_ID,
        status=DataStatus.PARTIAL if is_available else DataStatus.UNAVAILABLE,
        data=data,
        actual_source="redis_auction_projection+theme_mapping",
        requested_trade_date=trade_date,
        actual_trade_date=trade_date if is_available else None,
        effective_at_ms=None,
        available_at_ms=None,
        observed_at_ms=observed_at_ms,
        schema_version=1,
        completeness=(
            min(
                1.0,
                float(theme_shadow.get("mapping_count", 0))
                / max(1, int(theme_shadow.get("row_count", 0))),
            )
            if is_available
            else 0.0
        ),
        missing_fields=(
            ("full_market_coverage_unknown", "historical_available_at_unknown")
            if is_available
            else ("0924_or_0925_projection_missing_or_invalid",)
        ),
        provenance=provenance,
        temporal_mode="LIVE",
        fetch_completed_at_ms=observed_at_ms,
    )
    request = DataRequest(
        request_id=result.request_id,
        function_id=THEME_DELTA_FUNCTION_ID,
        trade_date=trade_date,
        effective_as_of_ms=knowledge_as_of_ms,
        knowledge_as_of_ms=knowledge_as_of_ms,
        purpose="POSTMARKET_ENGINE_WIRING_DIAGNOSTIC",
        temporal_mode="LIVE",
    )
    return TemporalDataGuard.check(result, request)


def run_engine_shadow_from_real_inputs(
    *,
    projections: Sequence[Any],
    theme_shadow: Mapping[str, Any],
    trade_date: str,
    symbol: str,
    evaluation_base_ms: int,
    redis_commands: Sequence[Mapping[str, str]],
    selected_symbol_basis: str = "EXPLICIT_OR_TEST_INPUT",
) -> Mapping[str, Any]:
    """Run one real three-anchor cohort through one Core Engine instance."""

    trade_date = _strict_trade_date(trade_date)
    symbol = _strict_symbol(symbol)
    if isinstance(evaluation_base_ms, bool) or not isinstance(evaluation_base_ms, int) or evaluation_base_ms <= 0:
        raise ValueError("evaluation_base_ms must be a positive epoch-millisecond integer")
    by_tag = {projection.tag: projection for projection in projections}
    missing_tags = [tag for tag in ANCHOR_ORDER if tag not in by_tag]
    if missing_tags:
        raise ValueError("missing auction tags: " + ",".join(missing_tags))
    if any(by_tag[tag].trade_date != trade_date for tag in ANCHOR_ORDER):
        raise ValueError("auction projection date differs from Engine session")

    projection_hashes = {
        tag: by_tag[tag].content_hash for tag in ANCHOR_ORDER
    }
    theme_projection_hashes = theme_shadow.get("projection_content_hashes", {})
    same_frozen_theme_cohort = all(
        theme_projection_hashes.get(tag) == projection_hashes[tag]
        for tag in ("0924", "0925")
    )
    if not same_frozen_theme_cohort:
        raise ValueError("theme facts and Engine inputs do not share the same Redis cohort")

    strategy = AuctionShadowStrategy(
        scope_id=symbol,
        start_trigger_id="AUCTION_0920",
        middle_trigger_id="AUCTION_0924",
        end_trigger_id="AUCTION_0925",
        previous_segment_id=f"redis_auction_{trade_date}_{symbol}_0920_to_0924",
        current_segment_id=f"redis_auction_{trade_date}_{symbol}_0924_to_0925",
        previous_coverage_status="PARTIAL",
        current_coverage_status="PARTIAL",
        theme_delta_function_id=THEME_DELTA_FUNCTION_ID,
    )
    engine = DeterministicEngine(
        MarketStateReducer(),
        WindowManager((WindowSpec("auction", 0, 10**15),)),
        strategy,
        session_id=trade_date,
        phase="POSTMARKET_DIAGNOSTIC",
    )

    theme_result = _theme_data_result(
        theme_shadow,
        trade_date=trade_date,
        knowledge_as_of_ms=evaluation_base_ms,
    )
    seq = 1
    current = None
    diagnostic_logical_times: dict[str, int] = {}
    for index, tag in enumerate(ANCHOR_ORDER):
        logical_time_ms = evaluation_base_ms + (index + 1) * 1000
        diagnostic_logical_times[tag] = logical_time_ms
        projection = build_engine_projection(
            by_tag[tag], trade_date=trade_date, symbol=symbol
        )
        engine.submit(
            EngineSignal(
                f"real-redis-market-{tag}-{symbol}",
                logical_time_ms,
                seq,
                SignalKind.MARKET_UPDATE,
                projection,
            )
        )
        seq += 1
        timer_payload: dict[str, Any] = {
            "trigger_id": f"AUCTION_{tag}",
            "business_anchor_time_ms": _business_anchor_ms(trade_date, tag),
            "diagnostic_evaluation_time_ms": logical_time_ms,
            "historical_available_at": "UNKNOWN",
        }
        if tag == "0925":
            timer_payload["data_requirements"] = (THEME_DELTA_FUNCTION_ID,)
        engine.submit(
            EngineSignal(
                f"real-redis-timer-{tag}-{symbol}",
                logical_time_ms,
                seq,
                SignalKind.TIMER,
                timer_payload,
            )
        )
        seq += 1
        current = engine.run_until_empty()
        if tag == "0925":
            if len(current.pending_evaluations) != 1:
                raise RuntimeError("expected one pending real 0925 Engine evaluation")
            pending = current.pending_evaluations[0]
            bundle = FrozenDataBundle.from_results(
                evaluation_id=pending.evaluation_id,
                knowledge_as_of_ms=pending.knowledge_as_of_ms,
                function_order=(THEME_DELTA_FUNCTION_ID,),
                results_by_function={THEME_DELTA_FUNCTION_ID: theme_result},
            )
            engine.submit(
                EngineSignal(
                    f"real-redis-data-ready-0925-{symbol}",
                    logical_time_ms + 1,
                    seq,
                    SignalKind.DATA_READY,
                    {"evaluation_id": pending.evaluation_id, "bundle": bundle},
                )
            )
            current = engine.run_until_empty()
    if current is None or not current.strategy_results:
        raise RuntimeError("real Engine session produced no strategy result")

    final_trace = current.strategy_results[-1].trace
    engine_theme = final_trace.get("theme_delta_shadow", {})
    direct_theme = (
        build_legacy_theme_delta_shadow_trace(theme_shadow.get("facts", ()))
        if theme_shadow.get("status") == "OBSERVED"
        else None
    )
    engine_theme_shadow = engine_theme.get("shadow")
    direct_matches = (
        canonical_json(engine_theme_shadow) == canonical_json(direct_theme)
        if direct_theme is not None
        else engine_theme_shadow is None
    )
    return {
        "contract_version": "RealThemeDeltaEngineShadowV1",
        "run_mode": "POSTMARKET_DIAGNOSTIC",
        "trade_date": trade_date,
        "symbol": symbol,
        "selected_symbol_basis": selected_symbol_basis,
        "input_scope": "TOP_AMOUNT_NOT_FULL_MARKET",
        "single_engine_session": True,
        "trigger_ids": ("AUCTION_0920", "AUCTION_0924", "AUCTION_0925"),
        "diagnostic_logical_times_ms": diagnostic_logical_times,
        "theme_data_status": theme_result.status.value,
        "theme_data_content_hash": theme_result.content_hash,
        "theme_data_observed_at_ms": theme_result.observed_at_ms,
        "projection_content_hashes": projection_hashes,
        "projection_inventory": {
            tag: {
                "status": by_tag[tag].status,
                "scope": by_tag[tag].scope,
                "row_count": len(by_tag[tag].rows),
                "observed_at_ms": by_tag[tag].observed_at_ms,
                "source_time_ms": by_tag[tag].meta.get("ts"),
            }
            for tag in ANCHOR_ORDER
        },
        "same_frozen_theme_cohort": same_frozen_theme_cohort,
        "engine_theme_shadow": engine_theme_shadow,
        "direct_theme_shadow": direct_theme,
        "engine_matches_direct_theme_shadow": direct_matches,
        "engine_fact_status": final_trace.get("fact_status"),
        "engine_decision_status": final_trace.get("decision_status"),
        "processed_signals": current.processed_signals,
        "strategy_result_count": len(current.strategy_results),
        "pending_evaluations": len(current.pending_evaluations),
        "redis_read_commands": tuple(redis_commands),
        "redis_write_commands": (),
        "td_commands": (),
        "rabbit_consumes": 0,
        "effects": (),
        "historical_available_at": "UNKNOWN",
        "rabbit_arrival_order": "UNKNOWN",
        "full_market_coverage": "NOT_CLAIMED_TOP_AMOUNT_ONLY",
        "normal_strategy_acceptance": "NOT_EVALUATED",
        "read_only": True,
    }


def run_real_theme_delta_engine_shadow(
    *,
    client: Any,
    trade_date: str,
    symbol: str | None = None,
) -> Mapping[str, Any]:
    """Read one real Redis cohort and route it through Core's Engine."""

    trade_date = _strict_trade_date(trade_date)
    redis = ReadOnlyRedis(client)
    projections = read_redis_auction_projection(
        redis,
        trade_date=trade_date,
        tags=ANCHOR_ORDER,
    )
    theme_shadow = build_real_theme_delta_strategy_shadow_from_projections(
        client=redis,
        trade_date=trade_date,
        projections=projections,
    )
    completed_at_ms = max(
        int(datetime.now(timezone.utc).timestamp() * 1000),
        int(theme_shadow["observed_at_ms"]),
        *(int(projection.observed_at_ms) for projection in projections),
    )
    selected_symbol, selected_symbol_basis = _choose_symbol(
        projections, requested_symbol=symbol
    )
    result = dict(
        run_engine_shadow_from_real_inputs(
            projections=projections,
            theme_shadow=theme_shadow,
            trade_date=trade_date,
            symbol=selected_symbol,
            evaluation_base_ms=completed_at_ms + 1,
            redis_commands=redis.commands,
            selected_symbol_basis=selected_symbol_basis,
        )
    )
    result.update(
        {
            "redis_observation_completed_at_ms": completed_at_ms,
            "theme_projection_status": theme_shadow.get("projection_status", {}),
            "theme_row_count": theme_shadow.get("row_count", 0),
            "theme_mapping_count": theme_shadow.get("mapping_count", 0),
            "theme_mapping_missing_count": theme_shadow.get(
                "mapping_missing_count", 0
            ),
            "theme_signal_counts": theme_shadow.get("signal_counts", {}),
            "side_effect_boundary": (
                "Redis HGETALL-only adapter + in-memory Core Engine; "
                "no Redis/TD/Rabbit writes or effects"
            ),
        }
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--symbol")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--redis-host", default=os.environ.get("REDIS_HOST", "127.0.0.1"))
    parser.add_argument("--redis-port", type=int, default=int(os.environ.get("REDIS_PORT", "6379")))
    parser.add_argument("--redis-db", type=int, default=int(os.environ.get("REDIS_DB", "0")))
    parser.add_argument("--redis-password", default=os.environ.get("REDIS_PASSWORD"))
    args = parser.parse_args()
    import redis  # type: ignore[import-not-found]

    client = redis.Redis(
        host=args.redis_host,
        port=args.redis_port,
        db=args.redis_db,
        password=args.redis_password,
        decode_responses=True,
        socket_timeout=5,
        socket_connect_timeout=5,
    )
    try:
        result = run_real_theme_delta_engine_shadow(
            client=client,
            trade_date=args.trade_date,
            symbol=args.symbol,
        )
    finally:
        client.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        output.write(canonical_json(result))
        output.write("\n")
    print(canonical_json(result))
    return 0 if result["engine_matches_direct_theme_shadow"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
