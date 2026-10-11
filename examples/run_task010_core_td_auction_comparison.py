"""Compare Core auction facts with a frozen TD projection and legacy oracle.

The runner reads files only. It does not connect to TD/Redis/Rabbit and does
not import any legacy project code. The external oracle is treated as data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from engine_core import (
    build_anchor_shadow_evidence,
    classify_td_auction_inactivity,
    normalize_auction_change_bp_to_pct,
)

try:
    from run_real_auction_shadow import build_snapshots_from_rows
    from run_anchor_delta_shadow import normalize_td_rows
except ModuleNotFoundError:
    from examples.run_real_auction_shadow import build_snapshots_from_rows
    from examples.run_anchor_delta_shadow import normalize_td_rows


TAGS = ("0920", "0924", "0925")
TRANSITIONS = (("0920", "0924"), ("0924", "0925"))
TD_FIELDS = (
    "ts",
    "px_milli",
    "chg_bp",
    "match_amt_yuan",
    "rest_bid_amt_yuan",
    "rest_ask_amt_yuan",
    "limit_state",
    "symbol",
    "trade_date",
    "auction_tag",
)
ANCHOR_FIELD_MAP = {
    "price_milli": ("price_milli", "price_milli"),
    "amount_yuan": ("amount_yuan", "auction_amount_yuan"),
    "rest_bid_yuan": ("rest_bid_yuan", "auction_bid_amount_yuan"),
    "rest_ask_yuan": ("rest_ask_yuan", "auction_ask_amount_yuan"),
}
TRANSITION_FIELDS = (
    "status",
    "direction",
    "labels",
    "amount_delta_yuan",
    "price_delta_milli",
    "rest_bid_delta_yuan",
    "rest_ask_delta_yuan",
    "pressure_delta_yuan",
    "amount_ratio",
    "withdrawal_yuan",
    "directional_pressure_yuan",
    "amount_reference_bucket",
    "reference_labels",
)
CORE_TRANSITION_FIELD_MAP = {
    "directional_pressure_yuan": "auction_directional_pressure_yuan",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def compare_value(left: Any, right: Any) -> str:
    if left is None or right is None:
        return "MATCH" if left is None and right is None else "MISMATCH"
    if isinstance(left, (int, float, Decimal)) and not isinstance(left, bool):
        if isinstance(right, (int, float, Decimal)) and not isinstance(right, bool):
            try:
                return "MATCH" if Decimal(str(left)) == Decimal(str(right)) else "MISMATCH"
            except InvalidOperation:
                return "UNKNOWN"
    return "MATCH" if left == right else "MISMATCH"


def aggregate_status(statuses: Mapping[str, str]) -> str:
    if any(value == "MISMATCH" for value in statuses.values()):
        return "MISMATCH"
    if any(value == "UNKNOWN" for value in statuses.values()):
        return "UNKNOWN"
    return "MATCH"


def canonical_inactivity_status(value: Any) -> str | None:
    """Map legacy lowercase labels and Core enum labels to one vocabulary."""

    return {
        "inactive": "INACTIVE",
        "INACTIVE": "INACTIVE",
        "non_inactive": "NON_INACTIVE",
        "NON_INACTIVE": "NON_INACTIVE",
        "unknown": "UNKNOWN",
        "UNKNOWN": "UNKNOWN",
    }.get(value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--td-rows", type=Path, required=True)
    parser.add_argument("--oracle", type=Path, required=True)
    parser.add_argument("--core-shadow", type=Path, required=True)
    parser.add_argument("--q2frame", type=Path, required=True)
    parser.add_argument("--expected-q2frame-sha256", required=True)
    parser.add_argument("--trade-date", default="2026-10-09")
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()

    td_sha = sha256(args.td_rows)
    q2_sha = sha256(args.q2frame)
    oracle_sha = sha256(args.oracle)
    core_shadow_sha = sha256(args.core_shadow)
    if q2_sha != args.expected_q2frame_sha256:
        raise SystemExit("Q2Frame hash does not match the pinned input")

    oracle = json.loads(args.oracle.read_text(encoding="utf-8"))
    if oracle.get("format") != "Task010AuctionAnchorOracleV2":
        raise SystemExit("oracle must include pinned per-row TD inactivity classifications")
    if oracle.get("source_identity", {}).get("input_sha256") != td_sha:
        raise SystemExit("oracle and Core inputs are not the same frozen TD file")
    core_report = json.loads(args.core_shadow.read_text(encoding="utf-8"))
    if core_report.get("q2frame", {}).get("sha256") != q2_sha:
        raise SystemExit("Core replay report does not reference the pinned Q2Frame")

    td_rows = read_jsonl(args.td_rows)
    counts: Counter[tuple[str, str]] = Counter(
        (str(row.get("symbol") or ""), str(row.get("auction_tag") or ""))
        for row in td_rows
    )
    grouped: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    duplicate_symbols: set[str] = set()
    for row in td_rows:
        symbol = str(row.get("symbol") or "")
        tag = str(row.get("auction_tag") or "")
        if counts[(symbol, tag)] > 1:
            duplicate_symbols.add(symbol)
        elif tag in TAGS:
            grouped[symbol][tag] = dict(row)

    # Keep the Q2 input's observed universe separate from the TD projection;
    # this is a set comparison, not a claim of full-market coverage.
    q2_symbols: set[str] = set()
    q2_updates = 0
    q2_frames = 0
    for line in args.q2frame.open("r", encoding="utf-8"):
        if not line.strip():
            continue
        frame = json.loads(line)
        q2_frames += 1
        updates = frame.get("q2_updates") or []
        q2_updates += len(updates)
        q2_symbols.update(str(item.get("symbol") or "") for item in updates if item.get("symbol"))

    valid_td_rows: list[dict[str, Any]] = []
    snapshots_by_symbol: dict[str, dict[str, dict[str, Any]]] = {}
    adapter_errors: dict[str, str] = {}
    for symbol, by_tag in grouped.items():
        if symbol in duplicate_symbols:
            continue
        if set(by_tag) != set(TAGS):
            continue
        rows = [dict(by_tag[tag]) for tag in TAGS]
        for row in rows:
            value = row.get("ts")
            if isinstance(value, str):
                row["ts"] = datetime.fromisoformat(value)
        try:
            snapshots = build_snapshots_from_rows(
                rows,
                trade_date=args.trade_date,
                symbol=symbol,
            )
        except (TypeError, ValueError, KeyError) as exc:
            adapter_errors[symbol] = type(exc).__name__
            continue
        snapshots_by_symbol[symbol] = {
            tag: dict(snapshots[tag].symbol_states[symbol]) for tag in TAGS
        }
        valid_td_rows.extend(rows)

    core_normalized = normalize_td_rows(valid_td_rows)
    core_transitions: dict[tuple[str, str, str], dict[str, Any]] = {}
    for from_tag, to_tag in TRANSITIONS:
        for fact in build_anchor_shadow_evidence(
            core_normalized,
            from_tag=from_tag,
            to_tag=to_tag,
        ):
            core_transitions[(str(fact.get("symbol") or ""), from_tag, to_tag)] = dict(fact)

    old_anchors = {
        (str(row.get("symbol") or ""), str(row.get("tag") or "")): row
        for row in oracle.get("anchors", [])
    }
    old_transitions = {
        (
            str(row.get("symbol") or ""),
            str(row.get("from_tag") or ""),
            str(row.get("to_tag") or ""),
        ): row
        for row in oracle.get("transitions", [])
    }
    q2_facts = {
        tag: core_report.get("ordered", {})
        .get("anchor_evidence", {})
        .get(tag, {})
        .get("auction_anchor_facts_by_symbol", {})
        for tag in TAGS
    }

    per_symbol: list[dict[str, Any]] = []
    anchor_counts: dict[str, Counter[str]] = {
        field: Counter() for field in ANCHOR_FIELD_MAP
    }
    transition_counts: dict[str, dict[str, Counter[str]]] = {
        f"{start}->{end}": {field: Counter() for field in TRANSITION_FIELDS}
        for start, end in TRANSITIONS
    }
    q2_td_counts: dict[str, Counter[str]] = {tag: Counter() for tag in TAGS}
    change_pct_counts: dict[str, Counter[str]] = {tag: Counter() for tag in TAGS}
    inactivity_counts: Counter[str] = Counter()

    symbols = sorted(set(grouped) | set(q2_symbols))
    for symbol in symbols:
        tags = grouped.get(symbol, {})
        symbol_status = (
            "AMBIGUOUS" if symbol in duplicate_symbols
            else "COMPLETE_TAG_SET" if set(tags) == set(TAGS)
            else "PARTIAL_TAG_SET"
        )
        symbol_item: dict[str, Any] = {
            "symbol": symbol,
            "td_tag_status": symbol_status,
            "in_q2frame_input": symbol in q2_symbols,
            "anchors": {},
            "transitions": {},
        }

        for tag in TAGS:
            old = old_anchors.get((symbol, tag))
            state = snapshots_by_symbol.get(symbol, {}).get(tag)
            td_row = tags.get(tag)
            comparisons: dict[str, str] = {}
            values: dict[str, Any] = {}
            for report_field, (old_field, core_field) in ANCHOR_FIELD_MAP.items():
                old_value = old.get(old_field) if old else None
                core_value = state.get(core_field) if state else None
                status = (
                    compare_value(old_value, core_value)
                    if old is not None and state is not None
                    else "UNKNOWN"
                )
                comparisons[report_field] = status
                anchor_counts[report_field][status] += 1
                values[report_field] = {
                    "legacy_normalized": old_value,
                    "core_td_snapshot": core_value,
                    "status": status,
                }

            q2_fact = q2_facts[tag].get(symbol)
            td_price = td_row.get("px_milli") if td_row else None
            if not q2_fact or not td_row or q2_fact.get("status") != "AVAILABLE" or td_price is None:
                q2_td_status = "UNKNOWN"
            else:
                q2_td_status = compare_value(q2_fact.get("price_milli"), td_price)
            q2_td_counts[tag][q2_td_status] += 1

            core_change_pct = (
                normalize_auction_change_bp_to_pct(td_row.get("chg_bp"))
                if td_row is not None
                else None
            )
            if old is None or td_row is None:
                change_pct_status = "UNKNOWN"
            else:
                change_pct_status = compare_value(old.get("change_pct"), core_change_pct)
            change_pct_counts[tag][change_pct_status] += 1

            if tag == "0925":
                core_inactivity = classify_td_auction_inactivity(td_row or {})
                legacy_inactivity = old.get("td_inactivity") if old else None
                legacy_inactivity_canonical = canonical_inactivity_status(legacy_inactivity)
                core_inactivity_canonical = canonical_inactivity_status(core_inactivity)
                inactivity_status = (
                    compare_value(legacy_inactivity_canonical, core_inactivity_canonical)
                    if legacy_inactivity_canonical is not None and core_inactivity_canonical is not None
                    else "UNKNOWN"
                )
                inactivity_counts[inactivity_status] += 1
            else:
                core_inactivity = None
                legacy_inactivity = None
                legacy_inactivity_canonical = None
                core_inactivity_canonical = None
                inactivity_status = "NOT_APPLICABLE"

            symbol_item["anchors"][tag] = {
                "legacy_vs_core_td_adapter": {
                    "status": aggregate_status(comparisons),
                    "fields": values,
                    "core_fields_not_consumed": ["change_pct", "limit_state"],
                },
                "core_q2_fact_vs_td_price": {
                    "status": q2_td_status,
                    "q2_fact_status": q2_fact.get("status") if q2_fact else "UNKNOWN",
                    "q2_price_milli": q2_fact.get("price_milli") if q2_fact else None,
                    "td_price_milli": td_price,
                    "comparison_scope": "cross-source diagnostic; not legacy production-output parity",
                },
                "change_pct_legacy_vs_core_normalizer": {
                    "legacy_change_pct": old.get("change_pct") if old else None,
                    "core_change_pct": core_change_pct,
                    "status": change_pct_status,
                },
                "td_inactivity_0925_legacy_vs_core": {
                    "scope": "per-row classification only; not effective-universe membership",
                    "legacy_raw": legacy_inactivity,
                    "legacy_canonical": legacy_inactivity_canonical,
                    "core_raw": core_inactivity,
                    "core_canonical": core_inactivity_canonical,
                    "comparison_normalization": "legacy lowercase and Core uppercase status labels mapped to shared uppercase vocabulary",
                    "status": inactivity_status,
                },
            }

        for from_tag, to_tag in TRANSITIONS:
            key = f"{from_tag}->{to_tag}"
            old = old_transitions.get((symbol, from_tag, to_tag))
            core = core_transitions.get((symbol, from_tag, to_tag))
            fields: dict[str, Any] = {}
            statuses: dict[str, str] = {}
            for field in TRANSITION_FIELDS:
                old_value = old.get(field) if old else None
                core_field = CORE_TRANSITION_FIELD_MAP.get(field, field)
                core_value = core.get(core_field) if core else None
                status = (
                    compare_value(old_value, core_value)
                    if old is not None and core is not None
                    else "UNKNOWN"
                )
                statuses[field] = status
                transition_counts[key][field][status] += 1
                fields[field] = {
                    "legacy": old_value,
                    "core": core_value,
                    "status": status,
                    "classification": "UNKNOWN" if status == "MISMATCH" else None,
                }
            symbol_item["transitions"][key] = {
                "status": aggregate_status(statuses),
                "fields": fields,
            }
        per_symbol.append(symbol_item)

    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    rows_path = out_dir / "core_comparison_by_symbol.jsonl"
    summary_path = out_dir / "core_comparison_summary.json"
    rows_bytes = "".join(
        json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        for item in per_symbol
    ).encode("utf-8")
    with rows_path.open("xb") as handle:
        handle.write(rows_bytes)

    tag_rows = Counter(str(row.get("auction_tag") or "") for row in td_rows)
    q2_td_tag_diff = {
        tag: {
            "td_symbols": len({symbol for symbol, item in grouped.items() if tag in item}),
            "q2_only_count": len(q2_symbols - {symbol for symbol, item in grouped.items() if tag in item}),
            "td_only_count": len({symbol for symbol, item in grouped.items() if tag in item} - q2_symbols),
        }
        for tag in TAGS
    }
    summary = {
        "format": "Task010CoreTDAuctionComparisonV1",
        "trade_date": args.trade_date,
        "inputs": {
            "td_rows_path": str(args.td_rows),
            "td_rows_sha256": td_sha,
            "legacy_oracle_path": str(args.oracle),
            "legacy_oracle_sha256": oracle_sha,
            "core_shadow_path": str(args.core_shadow),
            "core_shadow_sha256": core_shadow_sha,
            "q2frame_path": str(args.q2frame),
            "q2frame_sha256": q2_sha,
        },
        "source_inventory": {
            "td_rows": len(td_rows),
            "rows_by_tag": dict(tag_rows),
            "td_unique_symbols": len(grouped),
            "q2frame_frames": q2_frames,
            "q2frame_updates": q2_updates,
            "q2frame_unique_symbols": len(q2_symbols),
            "td_q2frame_symbol_difference": q2_td_tag_diff,
            "duplicate_symbol_tag_symbols": len(duplicate_symbols),
            "duplicate_symbols": sorted(duplicate_symbols),
            "core_adapter_symbols": len(snapshots_by_symbol),
            "core_adapter_errors": len(adapter_errors),
            "core_adapter_error_samples": dict(list(sorted(adapter_errors.items()))[:20]),
        },
        "legacy_vs_core_td_adapter": {
            "anchor_field_counts": {field: dict(counter) for field, counter in anchor_counts.items()},
            "transition_field_counts": {
                pair: {field: dict(counter) for field, counter in fields.items()}
                for pair, fields in transition_counts.items()
            },
            "not_compared_by_current_core_td_adapter": {
                "limit_state": "legacy normalizer exposes this; Core TD snapshot state does not",
                "effective_universe_membership": "UNKNOWN: legacy production assembly also needs Redis anchor membership; per-row TD inactivity is compared separately",
            },
        },
        "core_q2_fact_vs_td_price": {
            tag: dict(counter) for tag, counter in q2_td_counts.items()
        },
        "change_pct_legacy_vs_core_normalizer": {
            tag: dict(counter) for tag, counter in change_pct_counts.items()
        },
        "td_inactivity_0925_legacy_vs_core": dict(inactivity_counts),
        "market_completeness": "UNKNOWN; row counts and observed symbol-set equality do not prove a full-market universe",
        "legacy_output_scope": "exact legacy pure code on the same frozen TD rows; not 2026-10-09 engine-next production output",
        "arrival_order": "UNKNOWN",
        "historical_available_at": "UNKNOWN",
        "side_effects": "file-only Core processing; source freeze was TD SELECT only; no Redis/Rabbit/TD writes or service actions",
        "per_symbol_path": str(rows_path),
        "per_symbol_sha256": hashlib.sha256(rows_bytes).hexdigest(),
    }
    with summary_path.open("x", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
