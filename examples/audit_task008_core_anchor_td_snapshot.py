"""Compare Core's real 09:25 anchor facts with the same-date TD snapshot.

This is an offline, hash-pinned audit of retained source artifacts. It does
not connect to Redis, TDengine, RabbitMQ, or a production service. Timestamp
difference is reported as context and never used to reject otherwise equal
anchor values.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
TRADE_DATE = "2026-09-30"
ANCHOR_TAGS = ("0920", "0924", "0925")
CORE_REPORT = Path(
    "/home/exedev/validation/task008-field-delta-same-date-audit-20261008T044210+0800/"
    "core_q2frame_with_pressure_and_field_delta_20260930.json"
)
TD_ROWS = Path(
    "/home/exedev/validation/task008-same-date-production-assembly-20260930-20261002T054020+0800/"
    "td_auction_snapshot_rows.jsonl"
)
Q2FRAME = Path(
    "/home/exedev/validation/task008-same-day-t1-q2frame-20260930-to-0932-20261002T055725+0800/"
    "deployed_release_q2frame_to_0932.jsonl"
)
EXPECTED_SHA256 = {
    "core_report": "3d1000b522d960bd0081fbd657d907ffbda43dbb5db7ac6b3ae84e366359daa2",
    "td_rows": "162c259b54cde160a9ea3aa861b15ae399321889fa30f73456b376141b767b6d",
    "q2frame": "1f712b200fc22ab1dec7d328ee96d23799f3179521c455c39a1944589374d38a",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise ValueError(f"expected a JSON object: {path}")
    return dict(value)


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, Mapping):
                raise ValueError(f"expected a JSON object at {path}:{line_number}")
            yield dict(value)


def _compact_date(value: Any) -> str:
    return str(value or "").replace("-", "").strip()


def _positive_integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float) and math.isfinite(value) and value.is_integer() and value > 0:
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"\+?\d+", value.strip()):
        parsed = int(value)
        return parsed if parsed > 0 else None
    return None


def _td_price(value: Any) -> tuple[int | None, str]:
    if value is None:
        return None, "MISSING"
    parsed = _positive_integer(value)
    if parsed is None:
        return None, "INVALID_OR_NON_POSITIVE"
    return parsed, "AVAILABLE"


def _timestamp_ms(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    else:
        parsed = parsed.astimezone(SHANGHAI)
    return int(parsed.timestamp() * 1000)


def compare_core_anchor_to_td_rows(
    core_report: Mapping[str, Any],
    td_rows: Iterable[Mapping[str, Any]],
    *,
    trade_date: str,
    tag: str,
) -> dict[str, Any]:
    """Compare a Core anchor's value/status with captured TD snapshot rows.

    The expected cohort is the union of symbols in the Core anchor facts and
    TD rows for the requested date/tag. Missing rows stay visible; no zero-fill
    or subsecond time gate is applied.
    """

    if _compact_date(core_report.get("trade_date")) != _compact_date(trade_date):
        raise ValueError("Core report trade_date does not match comparison date")
    try:
        anchor = core_report["ordered"]["anchor_evidence"][tag]
        core_facts = anchor["auction_anchor_facts_by_symbol"]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"Core report has no per-symbol anchor facts for {tag}") from exc
    if not isinstance(core_facts, Mapping):
        raise ValueError("Core per-symbol anchor facts must be a mapping")

    core_by_symbol: dict[str, dict[str, Any]] = {}
    malformed_core_facts = 0
    for raw_symbol, raw_fact in core_facts.items():
        symbol = str(raw_symbol).strip()
        if not re.fullmatch(r"\d{6}", symbol) or not isinstance(raw_fact, Mapping):
            malformed_core_facts += 1
            continue
        if str(raw_fact.get("symbol", symbol)).strip() != symbol:
            malformed_core_facts += 1
            continue
        if str(raw_fact.get("tag", tag)) != tag:
            malformed_core_facts += 1
            continue
        core_by_symbol[symbol] = dict(raw_fact)

    td_by_symbol: dict[str, dict[str, Any]] = {}
    duplicate_td_rows = 0
    ignored_td_rows = 0
    for row in td_rows:
        if not isinstance(row, Mapping):
            ignored_td_rows += 1
            continue
        if str(row.get("auction_tag") or "").strip() != tag:
            continue
        if _compact_date(row.get("trade_date")) != _compact_date(trade_date):
            ignored_td_rows += 1
            continue
        symbol = str(row.get("symbol") or "").strip()
        if not re.fullmatch(r"\d{6}", symbol):
            ignored_td_rows += 1
            continue
        if symbol in td_by_symbol:
            duplicate_td_rows += 1
            continue
        price, price_status = _td_price(row.get("px_milli"))
        td_by_symbol[symbol] = {
            "price_milli": price,
            "price_status": price_status,
            "source_time_ms": _timestamp_ms(row.get("ts")),
        }

    counts: Counter[str] = Counter({
        "value_match": 0,
        "missing_match": 0,
        "value_mismatch": 0,
        "availability_mismatch": 0,
        "core_available_td_missing": 0,
        "td_available_core_unavailable": 0,
        "quality_difference": 0,
        "uncomparable_td_price": 0,
        "td_only_symbol": 0,
        "core_only_symbol": 0,
    })
    symbol_rows: list[dict[str, Any]] = []
    source_time_deltas: list[int] = []
    for symbol in sorted(set(core_by_symbol) | set(td_by_symbol)):
        core = core_by_symbol.get(symbol)
        td = td_by_symbol.get(symbol)
        if core is None:
            counts["td_only_symbol"] += 1
            symbol_rows.append({
                "tag": tag,
                "symbol": symbol,
                "comparison": "TD_ONLY_SYMBOL",
                "td_price_status": td["price_status"],
                "td_price_milli": td["price_milli"],
            })
            continue
        if td is None:
            counts["core_only_symbol"] += 1
            symbol_rows.append({
                "tag": tag,
                "symbol": symbol,
                "comparison": "CORE_ONLY_SYMBOL",
                "core_status": core.get("status"),
                "core_price_milli": core.get("price_milli"),
            })
            continue

        core_status = str(core.get("status") or "UNKNOWN").upper()
        core_price = _positive_integer(core.get("price_milli"))
        td_price = td["price_milli"]
        core_source_time = _timestamp_ms(core.get("source_time_ms"))
        if (
            core_status == "AVAILABLE"
            and core_price is not None
            and td["price_status"] == "AVAILABLE"
            and td["source_time_ms"] is not None
            and core_source_time is not None
        ):
            source_time_deltas.append(
                core_source_time - td["source_time_ms"]
            )

        if td["price_status"] == "INVALID_OR_NON_POSITIVE":
            comparison = "UNCOMPARABLE_TD_PRICE"
            counts["uncomparable_td_price"] += 1
        elif td["price_status"] == "MISSING":
            if core_status == "MISSING" and core.get("price_milli") is None:
                comparison = "MISSING_MATCH"
                counts["missing_match"] += 1
            elif core_price is None:
                comparison = "NULL_VALUE_QUALITY_DIFFERENCE"
                counts["quality_difference"] += 1
            else:
                comparison = "AVAILABILITY_MISMATCH"
                counts["availability_mismatch"] += 1
                counts["core_available_td_missing"] += 1
        elif core_status != "AVAILABLE" or core_price is None:
            comparison = "AVAILABILITY_MISMATCH"
            counts["availability_mismatch"] += 1
            counts["td_available_core_unavailable"] += 1
        elif core_price != td_price:
            comparison = "VALUE_MISMATCH"
            counts["value_mismatch"] += 1
        else:
            comparison = "VALUE_MATCH"
            counts["value_match"] += 1

        symbol_rows.append({
            "tag": tag,
            "symbol": symbol,
            "comparison": comparison,
            "core_status": core_status,
            "core_price_milli": core.get("price_milli"),
            "core_source_time_ms": _timestamp_ms(core.get("source_time_ms")),
            "td_price_status": td["price_status"],
            "td_price_milli": td_price,
            "td_source_time_ms": td["source_time_ms"],
        })

    mismatch_count = counts["value_mismatch"] + counts["availability_mismatch"]
    partial_count = (
        counts["td_only_symbol"]
        + counts["core_only_symbol"]
        + counts["uncomparable_td_price"]
        + malformed_core_facts
        + duplicate_td_rows
        + ignored_td_rows
    )
    quality_difference_count = counts["quality_difference"]
    if mismatch_count:
        status = "VALUE_MISMATCH"
    elif partial_count:
        status = "PARTIAL_COMPARISON"
    elif quality_difference_count:
        status = "VALUE_EQUAL_QUALITY_DIFFERENT"
    else:
        status = "CANONICAL_VALUE_PARITY"

    absolute_deltas = [abs(value) for value in source_time_deltas]
    core_timing = anchor.get("auction_revision", {})
    evaluation_time_ms = core_timing.get("evaluation_time_ms")
    observed_at_ms = core_timing.get("observed_at_ms")
    return {
        "contract": "Task008CoreAnchorVsTdSnapshotV1",
        "trade_date": trade_date,
        "tag": tag,
        "status": status,
        "comparison_scope": "per-symbol anchor price and availability only",
        "counts": {
            **dict(sorted(counts.items())),
            "core_symbol_count": len(core_by_symbol),
            "td_symbol_count": len(td_by_symbol),
            "compared_symbol_count": len(set(core_by_symbol) & set(td_by_symbol)),
            "malformed_core_facts": malformed_core_facts,
            "duplicate_td_rows": duplicate_td_rows,
            "ignored_td_rows": ignored_td_rows,
        },
        "timing_diagnostic": {
            "comparison": (
                "Core AuctionAnchorFact.source_time_ms minus captured TD row ts"
            ),
            "timestamp_semantics_equivalent": "NOT_ESTABLISHED",
            "arrival_latency_inferred": False,
            "core_evaluation_time_ms": evaluation_time_ms,
            "core_observed_at_ms": observed_at_ms,
            "core_observed_to_evaluation_ms": (
                evaluation_time_ms - observed_at_ms
                if type(evaluation_time_ms) is int and type(observed_at_ms) is int
                else None
            ),
            "paired_source_time_delta_count": len(source_time_deltas),
            "paired_available_anchor_count": len(source_time_deltas),
            "min_signed_source_time_delta_ms": (
                min(source_time_deltas) if source_time_deltas else None
            ),
            "max_signed_source_time_delta_ms": (
                max(source_time_deltas) if source_time_deltas else None
            ),
            "median_abs_source_time_delta_ms": (
                median(absolute_deltas) if absolute_deltas else None
            ),
            "max_abs_source_time_delta_ms": max(absolute_deltas) if absolute_deltas else None,
            "affects_status": False,
        },
        "symbol_comparisons": symbol_rows,
    }


def run_pinned_audit(
    output_dir: Path,
    *,
    tags: Iterable[str] = ANCHOR_TAGS,
) -> dict[str, Any]:
    selected_tags = tuple(dict.fromkeys(tags))
    if not selected_tags or set(selected_tags) - set(ANCHOR_TAGS):
        raise ValueError("tags must be a non-empty subset of 0920, 0924, 0925")
    paths = {
        "core_report": CORE_REPORT,
        "td_rows": TD_ROWS,
        "q2frame": Q2FRAME,
    }
    actual_hashes = {name: _sha256(path) for name, path in paths.items()}
    for name, expected in EXPECTED_SHA256.items():
        if actual_hashes[name] != expected:
            raise ValueError(f"pinned {name} SHA-256 mismatch")

    core = _read_json(CORE_REPORT)
    if core.get("q2frame", {}).get("sha256") != EXPECTED_SHA256["q2frame"]:
        raise ValueError("Core report does not identify the pinned Q2Frame input")
    td_rows = tuple(_iter_jsonl(TD_ROWS))
    tag_summaries: dict[str, Any] = {}
    symbol_comparisons: list[dict[str, Any]] = []
    for tag in selected_tags:
        comparison = compare_core_anchor_to_td_rows(
            core,
            td_rows,
            trade_date=TRADE_DATE,
            tag=tag,
        )
        tag_summaries[tag] = {
            key: value
            for key, value in comparison.items()
            if key not in {"symbol_comparisons", "contract", "trade_date", "tag"}
        }
        symbol_comparisons.extend(comparison["symbol_comparisons"])
    output_dir.mkdir(parents=True, exist_ok=False)
    summary = {
        "contract": "Task008CoreAnchorVsTdSnapshotBatchV1",
        "trade_date": TRADE_DATE,
        "status": "PER_TAG_DIAGNOSTICS_NO_CROSS_TAG_GATE",
        "interpretation": (
            "Per-tag value differences are observations only; they do not infer "
            "arrival cause or gate another anchor. 0920/0924 are optional inputs "
            "for 0925 analysis."
        ),
        "tag_results": tag_summaries,
        "inputs": {
            name: {"path": str(path), "sha256": actual_hashes[name]}
            for name, path in paths.items()
        },
    }
    summary_path = output_dir / "anchor_parity_summary.json"
    rows_path = output_dir / "symbol_comparisons.jsonl"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    with rows_path.open("x", encoding="utf-8") as handle:
        for row in symbol_comparisons:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    (output_dir / "sha256sums.txt").write_text(
        f"{_sha256(summary_path)}  anchor_parity_summary.json\n"
        f"{_sha256(rows_path)}  symbol_comparisons.jsonl\n",
        encoding="utf-8",
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument(
        "--tag",
        action="append",
        choices=ANCHOR_TAGS,
        help="anchor tag to compare; repeatable, defaults to all three",
    )
    args = parser.parse_args()
    summary = run_pinned_audit(args.output_dir, tags=args.tag or ANCHOR_TAGS)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
