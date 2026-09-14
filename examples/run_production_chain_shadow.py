"""Build a read-only production-chain audit bundle from captured artifacts.

This tool deliberately consumes *captured* files instead of connecting to a
live service.  The live connections are exercised by the existing bounded
probe scripts; this command makes their evidence comparable and runs the
same Q2 adapter/Engine path over the captured Redis cohort.  It never writes
Redis or TD, consumes Rabbit, repairs a missing auction slot, or sends an
effect.

The command is intentionally an evidence tool, not a runtime coordinator.
It reports a missing 0924 capture as missing and never synthesizes one from
0920/0925 or from a later Redis value.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine_core import (  # noqa: E402
    DeterministicEngine,
    EngineSignal,
    FreshnessPolicy,
    MarketStateReducer,
    ProbeStrategy,
    SignalKind,
    WindowManager,
    WindowSpec,
    build_q2_projection,
)
from examples.run_real_auction_shadow import build_shadow_from_rows  # noqa: E402


REQUIRED_AUCTION_SLOTS = ("auction_0920", "auction_0924", "auction_0925")
Q2_PREFIX = "q2_"
Q2_SUFFIX = ".jsonl"
def _json_value(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    if isinstance(value, str):
        return json.loads(value)
    return value


def _iso_to_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("capture timestamps must be timezone-aware")
    return int(parsed.astimezone(timezone.utc).timestamp() * 1000)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_manifest(capture_dir: Path) -> dict[str, Any]:
    path = capture_dir / "capture_manifest.partial.json"
    if not path.is_file():
        raise FileNotFoundError(path)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("capture manifest must be a JSON object")
    return value


def manifest_summary(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Recompute truth status instead of trusting the producer flag."""

    slots = {
        str(item.get("name")): item
        for item in manifest.get("slots", ())
        if isinstance(item, Mapping) and item.get("name")
    }
    missing = [
        name
        for name in REQUIRED_AUCTION_SLOTS
        if slots.get(name, {}).get("status") != "ON_TIME"
    ]
    if not missing:
        ground_truth_status = "COMPLETE"
    elif all(name in slots for name in missing):
        ground_truth_status = "PARTIAL"
    else:
        ground_truth_status = "OBSERVED"

    start = manifest.get("runtime_identity_start")
    end = manifest.get("runtime_identity_end")
    comparable_identity_fields = (
        "service",
        "main_pid",
        "binary_sha256",
        "release_git_commit",
        "build_info_sha256",
        "source_bundle_sha256",
        "exec_main_start_timestamp",
        "restart_count",
    )
    identity_equal = (
        isinstance(start, Mapping)
        and isinstance(end, Mapping)
        and all(
            field in start
            and field in end
            and start.get(field) is not None
            and end.get(field) is not None
            and start.get(field) == end.get(field)
            for field in comparable_identity_fields
        )
    )
    config_provenance = (
        "COMPLETE"
        if isinstance(start, Mapping)
        and isinstance(end, Mapping)
        and start.get("effective_config_sha256")
        and end.get("effective_config_sha256")
        else "PARTIAL"
    )
    return {
        "declared_formal_ground_truth": bool(manifest.get("formal_ground_truth")),
        "recomputed_ground_truth_status": ground_truth_status,
        "failed_required_slots": missing,
        "sealed": bool(manifest.get("sealed")),
        "td_export_status": manifest.get("td_export_status"),
        "release_provenance_valid": bool(manifest.get("release_provenance_valid")),
        "runtime_identity_observed_equal": identity_equal,
        "runtime_config_provenance": config_provenance,
        "runtime_identity_claim": (
            "STABLE_OBSERVED" if identity_equal else "CHANGED_OR_UNPROVEN"
        ),
        "write_isolation": manifest.get("write_isolation"),
    }


def load_q2_jsonl(path: Path) -> tuple[tuple[str, ...], dict[str, dict[str, Any]]]:
    """Load one captured Redis Q2 cohort without changing raw values."""

    rows: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"Q2 line {line_number} is not an object")
            symbol = str(value.get("symbol") or "").strip()
            if not symbol:
                raise ValueError(f"Q2 line {line_number} has no symbol")
            if symbol in rows:
                raise ValueError(f"duplicate Q2 symbol {symbol}")
            rows[symbol] = value
    symbols = tuple(sorted(rows))
    return symbols, rows


def run_captured_q2_engine(
    *,
    trade_date: str,
    q2_path: Path,
    observed_at_ms: int,
    stale_after_ms: int,
) -> dict[str, Any]:
    symbols, rows = load_q2_jsonl(q2_path)
    observed_at = datetime.fromtimestamp(observed_at_ms / 1000, tz=timezone.utc)
    projection = build_q2_projection(
        trade_date,
        observed_at,
        symbols,
        rows,
        freshness_policy=FreshnessPolicy(stale_after_ms=stale_after_ms),
        source_id="capture://redis_q2_projection",
    )
    engine = DeterministicEngine(
        MarketStateReducer(),
        WindowManager((WindowSpec("observation", observed_at_ms, observed_at_ms + 1),)),
        ProbeStrategy(),
        session_id=trade_date,
        phase="READ_ONLY_SHADOW",
    )
    engine.submit(
        EngineSignal(
            "captured-q2-update",
            observed_at_ms,
            1,
            SignalKind.MARKET_UPDATE,
            projection,
        )
    )
    engine.submit(
        EngineSignal(
            "captured-q2-probe",
            observed_at_ms + 1,
            2,
            SignalKind.TIMER,
            {"trigger_id": "CAPTURED_Q2_PROBE", "close_windows": ("observation",)},
        )
    )
    first = engine.run_until_empty()
    # A second independent run proves the captured business result is stable.
    second_engine = DeterministicEngine(
        MarketStateReducer(),
        WindowManager((WindowSpec("observation", observed_at_ms, observed_at_ms + 1),)),
        ProbeStrategy(),
        session_id=trade_date,
        phase="READ_ONLY_SHADOW",
    )
    second_engine.submit(
        EngineSignal("captured-q2-update", observed_at_ms, 1, SignalKind.MARKET_UPDATE, projection)
    )
    second_engine.submit(
        EngineSignal(
            "captured-q2-probe",
            observed_at_ms + 1,
            2,
            SignalKind.TIMER,
            {"trigger_id": "CAPTURED_Q2_PROBE", "close_windows": ("observation",)},
        )
    )
    second = second_engine.run_until_empty()
    first_probe = first.strategy_results[-1]
    second_probe = second.strategy_results[-1]
    return {
        "source_file": q2_path.name,
        "observed_at_ms": observed_at_ms,
        "symbol_count": len(symbols),
        "quote_count": len(projection.quotes),
        "coverage": projection.coverage,
        "status": projection.status.value,
        "consistency_status": projection.consistency_status,
        "missing_symbol_count": len(projection.missing_symbols),
        "stale_symbol_count": len(projection.stale_symbols),
        "oldest_source_time_ms": projection.oldest_source_time_ms,
        "newest_source_time_ms": projection.newest_source_time_ms,
        "projection_hash": projection.content_hash,
        "engine_processed_signals": first.processed_signals,
        "engine_snapshot_hashes": [item.content_hash for item in first.snapshots],
        "engine_probe_hash": first_probe.content_hash,
        "repeat_engine_probe_hash": second_probe.content_hash,
        "repeat_hash_equal": first_probe.content_hash == second_probe.content_hash,
        "side_effect_boundary": "captured file read + in-memory Core only",
    }


def auction_summary(capture_dir: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    slots = {
        str(item.get("name")): item
        for item in manifest.get("slots", ())
        if isinstance(item, Mapping) and item.get("name")
    }
    for slot in ("auction_0920", "auction_0924", "auction_0925"):
        path = capture_dir / f"{slot}.json"
        slot_meta = slots.get(slot, {})
        if not path.is_file():
            result[slot] = {
                "status": "MISSING",
                "capture_status": slot_meta.get("status", "UNKNOWN"),
                "error": slot_meta.get("error", "artifact absent"),
            }
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        latest = payload.get("latest") if isinstance(payload, Mapping) else None
        if not isinstance(latest, Mapping):
            raise ValueError(f"{path.name} has no latest projection")
        summary = _json_value(latest.get("summary"))
        top_amount = _json_value(latest.get("top_amount"))
        result[slot] = {
            "status": "OBSERVED",
            "projection_only": True,
            "capture_status": slot_meta.get("status", "UNKNOWN"),
            "source_key": payload.get("source_key"),
            "latest_tag": latest.get("tag"),
            "latest_source_record_time_ms": latest.get("ts"),
            "meta": _json_value(latest.get("meta")),
            "summary": summary,
            "top_amount_count": len(top_amount) if isinstance(top_amount, list) else None,
            "top_amount_symbols": sorted(
                str(row.get("symbol"))
                for row in (top_amount if isinstance(top_amount, list) else ())
                if isinstance(row, Mapping) and row.get("symbol")
            ),
            "anchor_present": bool(payload.get("anchor")),
            "file_sha256": _sha256(path),
        }
    anchor_path = capture_dir / "auction_anchor.json"
    result["auction_anchor"] = {
        "status": "OBSERVED" if anchor_path.is_file() else "MISSING",
        "file_sha256": _sha256(anchor_path) if anchor_path.is_file() else None,
    }
    return result


def _auction_source_rows(path: Path) -> tuple[str, list[dict[str, Any]], Mapping[str, Any]]:
    """Load raw/canonical auction rows, never a precomputed fact result."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("auction source artifact must be a JSON object")
    rows_value = payload.get("rows")
    if isinstance(rows_value, list):
        raw_rows = rows_value
    else:
        anchors = payload.get("anchors")
        if not isinstance(anchors, Mapping):
            raise ValueError("auction source artifact has no rows or anchors")
        raw_rows = []
        for tag in ("0920", "0924", "0925"):
            anchor = anchors.get(tag)
            if not isinstance(anchor, Mapping):
                raise ValueError(f"auction source anchor {tag} is missing")
            raw_rows.append(anchor.get("raw"))
    if not raw_rows or any(not isinstance(row, Mapping) for row in raw_rows):
        raise ValueError("auction source rows must be mappings")
    rows: list[dict[str, Any]] = []
    for row in raw_rows:
        normalized = dict(row)
        source_time = normalized.get("ts")
        if isinstance(source_time, str):
            normalized["ts"] = datetime.fromisoformat(source_time.replace("Z", "+00:00"))
        rows.append(normalized)
    symbols = {str(row.get("symbol") or "") for row in rows}
    symbols.discard("")
    if len(symbols) != 1:
        raise ValueError("auction source rows must contain exactly one symbol")
    return next(iter(symbols)), rows, payload


def run_captured_auction_fact(
    path: Path,
    *,
    trade_date: str,
) -> dict[str, Any]:
    """Recompute the Core fact from real source rows and retain its evidence."""

    symbol, rows, source_payload = _auction_source_rows(path)
    recomputed = build_shadow_from_rows(rows, trade_date=trade_date, symbol=symbol)
    stored_shadow = source_payload.get("shadow")
    if isinstance(stored_shadow, Mapping):
        if stored_shadow.get("content_hash") != recomputed["shadow"].get("content_hash"):
            raise ValueError("stored auction shadow does not match source-row recomputation")
    shadow = recomputed["shadow"]
    return {
        "status": "OBSERVED",
        "source_file": path.name,
        "source_table": recomputed.get("source_table"),
        "source_semantics": recomputed.get("source_semantics"),
        "symbol": symbol,
        "anchor_tags": ("0920", "0924", "0925"),
        "segment_count": len(recomputed.get("segments", ())),
        "shadow_status": shadow.get("status"),
        "decision_status": shadow.get("decision_status"),
        "state": shadow.get("state"),
        "content_hash": shadow.get("content_hash"),
        "evidence_hash": shadow.get("evidence_hash"),
        "comparison_hash": shadow.get("comparison_hash"),
        "source_row_count": len(rows),
        "source_observed_at": source_payload.get("observed_at"),
        "engine_connected": False,
        "file_sha256": _sha256(path),
    }


def tick_shape_features(path: Path) -> list[dict[str, Any]]:
    """Extract morphology only; never infer causal ordering or phase."""

    result: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, Mapping):
                raise ValueError(f"tick line {line_number} is not an object")
            missing = [field for field in ("ts", "symbol") if row.get(field) in (None, "")]
            if missing:
                raise ValueError(f"tick line {line_number} missing {missing}")
            bid_prices = [row.get(f"bp{i}_milli") for i in range(1, 6)]
            ask_prices = [row.get(f"ap{i}_milli") for i in range(1, 6)]
            bid_volumes = [row.get(f"bv{i}") for i in range(1, 6)]
            ask_volumes = [row.get(f"av{i}") for i in range(1, 6)]
            result.append(
                {
                    "line_number": line_number,
                    "symbol": str(row["symbol"]),
                    "source_record_time": str(row["ts"]),
                    "source_record_time_ms": _iso_to_ms(str(row["ts"])),
                    "bid1_eq_ask1": bid_prices[0] == ask_prices[0],
                    "nonzero_bid_price_levels": sum(value not in (None, 0) for value in bid_prices),
                    "nonzero_ask_price_levels": sum(value not in (None, 0) for value in ask_prices),
                    "nonzero_bid_volume_levels": sum(value not in (None, 0) for value in bid_volumes),
                    "nonzero_ask_volume_levels": sum(value not in (None, 0) for value in ask_volumes),
                    "full_bid5_visible": all(value not in (None, 0) for value in bid_prices),
                    "full_ask5_visible": all(value not in (None, 0) for value in ask_prices),
                    "last_price_valid": row.get("px_milli") not in (None, 0),
                    "volume_nonzero": row.get("vol_units") not in (None, 0),
                    "amount_nonzero": row.get("amt_yuan") not in (None, 0),
                    "classification": "UNCLASSIFIED",
                    "causal_order": "UNKNOWN",
                }
            )
    return result


def _write_json(path: Path, value: Any) -> None:
    _write_text_lf(
        path,
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )


def _write_text_lf(path: Path, text: str) -> None:
    """Write comparable UTF-8 evidence with platform-independent LF endings."""

    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _write_matrix(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    fieldnames = ("trade_date", "anchor", "layer", "status", "detail", "evidence_ref")
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows({field: row.get(field, "") for field in fieldnames} for row in rows)


def build_audit_bundle(
    capture_dir: Path,
    output_dir: Path,
    *,
    trade_date: str,
    stale_after_ms: int,
    q2_file: str | None = None,
    tick_file: Path | None = None,
    auction_shadow_file: Path | None = None,
) -> dict[str, Any]:
    manifest = load_manifest(capture_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    q2_candidates = sorted(capture_dir.glob(f"{Q2_PREFIX}*{Q2_SUFFIX}"))
    if not q2_candidates:
        raise FileNotFoundError("no captured q2_*.jsonl files")
    q2_path = capture_dir / q2_file if q2_file else q2_candidates[-1]
    if not q2_path.is_file():
        raise FileNotFoundError(q2_path)
    q2_slot = next(
        (
            item
            for item in manifest.get("slots", ())
            if isinstance(item, Mapping) and q2_path.name in item.get("artifacts", ())
        ),
        None,
    )
    if not isinstance(q2_slot, Mapping) or not q2_slot.get("actual_capture_time"):
        raise ValueError(f"manifest has no capture time for {q2_path.name}")
    observed_at_ms = _iso_to_ms(str(q2_slot["actual_capture_time"]))
    manifest_result = manifest_summary(manifest)
    auctions = auction_summary(capture_dir, manifest)
    auction_fact = (
        run_captured_auction_fact(auction_shadow_file, trade_date=trade_date)
        if auction_shadow_file is not None
        else {
            "status": "NOT_RUN",
            "reason": "no raw/canonical auction source-row artifact supplied",
            "engine_connected": False,
        }
    )
    q2_result = run_captured_q2_engine(
        trade_date=trade_date,
        q2_path=q2_path,
        observed_at_ms=observed_at_ms,
        stale_after_ms=stale_after_ms,
    )

    tick_path = tick_file or (capture_dir / "td_stock_tick_092450_093001.jsonl")
    tick_rows = tick_shape_features(tick_path) if tick_path.is_file() else []
    with (output_dir / "tick_shape_samples.jsonl").open(
        "w", encoding="utf-8", newline="\n"
    ) as handle:
        for row in tick_rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    transition_rows = [
        {
            "symbol": row["symbol"],
            "source_record_time_ms": row["source_record_time_ms"],
            "classification": row["classification"],
            "causal_order": row["causal_order"],
            "full_bid5_visible": row["full_bid5_visible"],
            "full_ask5_visible": row["full_ask5_visible"],
        }
        for row in tick_rows
    ]
    with (output_dir / "tick_shape_transition.csv").open("w", encoding="utf-8", newline="") as handle:
        fields = tuple(transition_rows[0].keys()) if transition_rows else (
            "symbol", "source_record_time_ms", "classification", "causal_order",
            "full_bid5_visible", "full_ask5_visible",
        )
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(transition_rows)
    counters = Counter(row["classification"] for row in tick_rows)
    _write_text_lf(
        output_dir / "tick_shape_statistics.csv",
        "classification,count\n" + "".join(
            f"{key},{value}\n" for key, value in sorted(counters.items())
        ),
    )

    matrix_rows = []
    for anchor, auction in auctions.items():
        for layer in (
            "Gateway/Runtime",
            "AuctionState",
            "Redis",
            "TD",
            "engine-next",
            "engine_core",
        ):
            if layer == "Gateway/Runtime":
                status, detail = "UNKNOWN", "capture has no Rabbit batch membership"
            elif layer == "AuctionState":
                status, detail = auction.get("status", "UNKNOWN"), (
                    "captured Redis projection only; internal AuctionState/freeze not observed"
                )
            elif layer == "Redis":
                status, detail = auction.get("status", "UNKNOWN"), "auction/anchor capture"
            elif layer == "TD":
                status = "OBSERVED" if tick_rows else "UNKNOWN"
                detail = "normalized stock_tick sample; not as-of aligned to this auction anchor"
            elif layer == "engine-next":
                status, detail = "UNKNOWN", "no read-only loader trace in capture"
            else:
                status, detail = (
                    ("OBSERVED", "real source-row AuctionFactShadow; not Engine-connected")
                    if auction_fact.get("status") == "OBSERVED"
                    else (
                        "UNPROVEN",
                        "Q2 shadow is not linked to this auction anchor; auction fact path not run",
                    )
                )
            matrix_rows.append({
                "trade_date": trade_date,
                "anchor": anchor,
                "layer": layer,
                "status": status,
                "detail": detail,
                "evidence_ref": f"capture://production_ground_truth/{trade_date}/{anchor}",
            })
    # Keep the Q2 Engine result in its own evidence scope.  It is a real,
    # deterministic shadow, but it is not evidence that the auction anchors
    # passed through the internal AuctionState/finalization path.
    q2_matrix_status = {
        "Gateway/Runtime": ("UNKNOWN", "capture has no Rabbit batch membership"),
        "AuctionState": ("UNKNOWN", "Q2 capture has no auction-state evidence"),
        "Redis": ("OBSERVED", "captured Redis Q2 cohort"),
        "TD": (
            "OBSERVED" if tick_rows else "UNKNOWN",
            "normalized stock_tick sample; no as-of join to Q2 performed",
        ),
        "engine-next": ("UNKNOWN", "no read-only loader trace in capture"),
        "engine_core": (
            "PASS" if q2_result["repeat_hash_equal"] else "FAIL",
            "captured Q2 adapter + in-memory engine shadow",
        ),
    }
    for layer, (status, detail) in q2_matrix_status.items():
        matrix_rows.append({
            "trade_date": trade_date,
            "anchor": "q2_capture",
            "layer": layer,
            "status": status,
            "detail": detail,
            "evidence_ref": f"capture://production_ground_truth/{trade_date}/q2",
        })
    _write_matrix(output_dir / "production_chain_matrix.csv", matrix_rows)

    audit_lines = [
        f"# Tick Shape Audit — {trade_date}",
        "",
        "status: OBSERVED (morphology extracted; phase predicate not frozen)",
        "",
        "- Raw vendor/Rabbit batch membership: UNKNOWN; this capture contains normalized TD rows only.",
        f"- Normalized TD rows analyzed: {len(tick_rows)}.",
        "- Same timestamp causal order: UNKNOWN; no source ordering key was present.",
        "- 09:25 final Tick inclusion in the original runtime batch: UNKNOWN.",
        "- The capture's 0924 Redis slot is missing; no 0924 state was synthesized.",
        "- Current Redis/TD values observed later are not retroactively assigned to the capture slot.",
        "",
        "## Questions requiring a future runtime audit hook",
        "",
        "1. Auction five-level morphology: OBSERVED in normalized TD samples; not yet a universal predicate.",
        "2. Post-auction morphology: UNKNOWN for full population; captured samples show both sparse and full levels.",
        "3. Candidate transition fields: level visibility, price validity, volume and amount presence.",
        "4. Post-09:25 auction-shaped ticks: UNKNOWN from current artifact scope.",
        "5. 09:25:06 region classification: OBSERVED only for TD projection rows, not source batch.",
        "6. lastPrice semantic transition: UNKNOWN as a producer contract.",
        "7. volume/amount semantic transition: UNKNOWN as a producer contract.",
        "8. AuctionCalculator formula parity: source-formula evidence only; consumer oracle pending.",
        "9. 0925 freeze ordering: UNKNOWN without runtime batch evidence.",
        "10. Market/special-stock differences: UNKNOWN beyond the bounded sample.",
    ]
    _write_text_lf(output_dir / "tick_shape_audit.md", "\n".join(audit_lines) + "\n")

    # Directory names are machine-local (for example ``capture-20260914``
    # versus ``20260914``) and must not participate in comparable evidence.
    # Prefer the producer's stable run identity; the trade date is the safe
    # fallback for older manifests that did not record one.
    capture_run_id = str(manifest.get("run_id") or f"capture:{trade_date}")
    summary = {
        "trade_date": trade_date,
        "capture_run_id": capture_run_id,
        "capture_manifest_sha256": _sha256(capture_dir / "capture_manifest.partial.json"),
        "manifest": manifest_result,
        "auction": auctions,
        "q2_engine_shadow": q2_result,
        "auction_fact_shadow": {
            **auction_fact,
            "0924_status": auctions.get("auction_0924", {}).get("status"),
        },
        "tick_shape": {
            "status": "OBSERVED" if tick_rows else "UNKNOWN",
            "row_count": len(tick_rows),
            "same_timestamp_order": "UNKNOWN",
            "source_file": tick_path.name if tick_path.is_file() else None,
        },
        "acceptance": {
            "source_ingestion": "UNKNOWN",
            "auction_state": "OBSERVED" if auctions.get("auction_0920", {}).get("status") == "OBSERVED" and auctions.get("auction_0925", {}).get("status") == "OBSERVED" else "UNKNOWN",
            "storage_projection": "WARN",
            "engine_next_consumption": "UNKNOWN",
            "engine_core_q2_path": "PASS" if q2_result["repeat_hash_equal"] else "FAIL",
            "engine_core_auction_fact": auction_fact.get("status", "NOT_RUN"),
            "engine_core_shadow": "PARTIAL" if q2_result["repeat_hash_equal"] else "FAIL",
            "joint_trading_day": "WARN",
        },
        "read_only": True,
        "side_effect_boundary": "local captured files + in-memory Core only",
    }
    _write_json(output_dir / "audit_summary.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--stale-after-ms", type=int, required=True)
    parser.add_argument("--q2-file")
    parser.add_argument("--tick-file", type=Path)
    parser.add_argument("--auction-shadow-file", type=Path)
    args = parser.parse_args()
    if args.stale_after_ms < 0:
        parser.error("stale-after-ms must be nonnegative")
    summary = build_audit_bundle(
        args.capture_dir,
        args.output_dir,
        trade_date=args.trade_date,
        stale_after_ms=args.stale_after_ms,
        q2_file=args.q2_file,
        tick_file=args.tick_file,
        auction_shadow_file=args.auction_shadow_file,
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
