"""Compare same-date TD auction rows with Q2Frame source-time candidates.

This is a file-only, event-time diagnostic over sealed real artifacts. It
truncates source timestamps to Shanghai whole seconds, retains explicit NULL
and malformed values, and reports differences without imposing a parity gate.
It does not reconstruct Rabbit arrival, Redis availability, or wall-clock
visibility.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections import defaultdict
from datetime import datetime, time
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
SESSION_START = time(9, 15, 0)
FIELD_MAP = {
    "am": "match_amt_yuan",
    "br": "rest_bid_amt_yuan",
    "ar": "rest_ask_amt_yuan",
}
ANCHOR_TAGS = ("0920", "0924", "0925")
SOURCE_GAP_BUCKETS = ("0", "1-3", "4-6", ">6")
DEFAULT_Q2FRAME = Path(
    "/home/exedev/validation/task008-same-day-release-replay-20260930T1018+0800/"
    "q2frame.jsonl"
)
DEFAULT_TD_ROWS = Path(
    "/home/exedev/validation/task008-same-date-production-assembly-20260930-"
    "20261002T054020+0800/td_auction_snapshot_rows.jsonl"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _compact_date(value: Any) -> str:
    return str(value or "").replace("-", "").strip()


def _parse_timestamp_ms(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
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


def _integer(value: Any) -> tuple[int | None, str]:
    if value is None:
        return None, "NULL"
    if isinstance(value, bool):
        return None, "INVALID"
    if isinstance(value, int):
        return value, "PRESENT"
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value), "PRESENT"
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        return int(value), "PRESENT"
    return None, "INVALID"


def _source_gap_bucket(gap_seconds: int) -> str:
    if gap_seconds == 0:
        return "0"
    if gap_seconds <= 3:
        return "1-3"
    if gap_seconds <= 6:
        return "4-6"
    return ">6"


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at {path}:{line_number}") from exc
            if not isinstance(value, Mapping):
                raise ValueError(f"expected JSON object at {path}:{line_number}")
            yield dict(value)


def compare_td_anchor_fields_to_q2frame(
    td_rows: Iterable[Mapping[str, Any]],
    q2_frames: Iterable[Mapping[str, Any]],
    *,
    trade_date: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Compare TD anchor fields with the last prior Q2 source-second candidate.

    For each `(tag, symbol)`, the candidate cutoff is the TD row's source
    timestamp truncated to a Shanghai whole second. The selected Q2 candidate is the
    greatest source second in `[09:15:00, cutoff]`; if multiple artifact
    updates occupy that same source second, the later Q2Frame artifact item
    wins. This describes event-time candidate alignment only.
    """

    expected_date = _compact_date(trade_date)
    if not re.fullmatch(r"\d{8}", expected_date):
        raise ValueError("trade_date must be YYYY-MM-DD or YYYYMMDD")

    diagnostics = {
        "td_rows": 0,
        "td_rows_ignored": 0,
        "duplicate_td_tag_symbol_rows": 0,
        "td_wrong_date_rows": 0,
        "td_invalid_symbol_rows": 0,
        "td_invalid_timestamp_rows": 0,
        "q2_frames": 0,
        "q2_updates": 0,
        "q2_malformed_frames": 0,
        "q2_malformed_updates": 0,
        "q2_wrong_date_updates": 0,
        "q2_invalid_symbol_updates": 0,
        "q2_invalid_timestamp_updates": 0,
    }
    td_by_tag: dict[str, dict[str, dict[str, Any]]] = {
        tag: {} for tag in ANCHOR_TAGS
    }
    for row in td_rows:
        diagnostics["td_rows"] += 1
        tag = str(row.get("auction_tag") or "").strip()
        if tag not in td_by_tag:
            diagnostics["td_rows_ignored"] += 1
            continue
        if _compact_date(row.get("trade_date")) != expected_date:
            diagnostics["td_wrong_date_rows"] += 1
            continue
        symbol = str(row.get("symbol") or "").strip()
        if not re.fullmatch(r"\d{6}", symbol):
            diagnostics["td_invalid_symbol_rows"] += 1
            continue
        timestamp_ms = _parse_timestamp_ms(row.get("ts"))
        if timestamp_ms is None:
            diagnostics["td_invalid_timestamp_rows"] += 1
            continue
        if symbol in td_by_tag[tag]:
            diagnostics["duplicate_td_tag_symbol_rows"] += 1
            continue
        source_second_ms = timestamp_ms // 1000 * 1000
        td_by_tag[tag][symbol] = {
            "td_timestamp_ms": timestamp_ms,
            "td_source_second_ms": source_second_ms,
            "fields": {
                td_field: _integer(row.get(td_field))
                for td_field in FIELD_MAP.values()
            },
        }

    # Per-symbol/tag candidates are bounded by the actual TD anchor cohort, not the
    # full Q2Frame event history. Q2 JSONL itself is consumed frame-by-frame.
    latest: dict[str, dict[str, dict[str, Any]]] = {
        tag: {} for tag in ANCHOR_TAGS
    }
    q2_frames_iter = iter(q2_frames)
    for frame in q2_frames_iter:
        diagnostics["q2_frames"] += 1
        updates = frame.get("q2_updates") if isinstance(frame, Mapping) else None
        if not isinstance(updates, list):
            diagnostics["q2_malformed_frames"] += 1
            continue
        for update in updates:
            diagnostics["q2_updates"] += 1
            if not isinstance(update, Mapping):
                diagnostics["q2_malformed_updates"] += 1
                continue
            symbol = str(update.get("symbol") or "").strip()
            if not re.fullmatch(r"\d{6}", symbol):
                diagnostics["q2_invalid_symbol_updates"] += 1
                continue
            timestamp_ms = _parse_timestamp_ms(update.get("ts"))
            if timestamp_ms is None:
                diagnostics["q2_invalid_timestamp_updates"] += 1
                continue
            local = datetime.fromtimestamp(timestamp_ms / 1000, SHANGHAI)
            if local.strftime("%Y%m%d") != expected_date:
                diagnostics["q2_wrong_date_updates"] += 1
                continue
            local_time = local.time().replace(tzinfo=None)
            if local_time < SESSION_START:
                continue
            source_second_ms = timestamp_ms // 1000 * 1000
            for tag in ANCHOR_TAGS:
                td = td_by_tag[tag].get(symbol)
                if td is None or source_second_ms > td["td_source_second_ms"]:
                    continue
                previous = latest[tag].get(symbol)
                if previous is not None and source_second_ms < previous["source_second_ms"]:
                    continue
                # Same-second ties deliberately use later artifact order after
                # truncation; source milliseconds are retained for diagnostics.
                latest[tag][symbol] = {
                    "source_timestamp_ms": timestamp_ms,
                    "source_second_ms": source_second_ms,
                    "fields": {
                        q2_field: _integer(update.get(q2_field))
                        for q2_field in FIELD_MAP
                    },
                }

    field_reports: dict[str, dict[str, dict[str, Any]]] = {}
    source_gap_counts: dict[str, dict[str, int]] = {}
    symbol_evidence: list[dict[str, Any]] = []
    for tag in ANCHOR_TAGS:
        field_reports[tag] = {}
        source_gap_counts[tag] = {"0": 0, "1-3": 0, "4-6": 0, ">6": 0}
        for q2_field, td_field in FIELD_MAP.items():
            field_reports[tag][q2_field] = {
                "td_field": td_field,
                "equal": 0,
                "different": 0,
                "compared_count": 0,
                "td_null": 0,
                "td_invalid": 0,
                "no_q2_candidate": 0,
                "q2_null": 0,
                "q2_invalid": 0,
                "td_value_sum": 0,
                "q2_value_sum": 0,
                "signed_difference_sum": 0,
                "absolute_difference_sum": 0,
                "absolute_difference_max": 0,
                "mismatch_samples": [],
                "by_source_time_gap_seconds": {
                    bucket: {
                        "equal": 0,
                        "different": 0,
                        "td_null": 0,
                        "td_invalid": 0,
                        "q2_null": 0,
                        "q2_invalid": 0,
                    }
                    for bucket in SOURCE_GAP_BUCKETS
                },
            }

        for symbol in sorted(td_by_tag[tag]):
            td = td_by_tag[tag][symbol]
            q2 = latest[tag].get(symbol)
            row_evidence: dict[str, Any] = {
                "auction_tag": tag,
                "symbol": symbol,
                "td_timestamp_ms": td["td_timestamp_ms"],
                "td_source_second_ms": td["td_source_second_ms"],
                "q2_candidate_source_timestamp_ms": (
                    q2["source_timestamp_ms"] if q2 else None
                ),
                "q2_candidate_source_second_ms": (
                    q2["source_second_ms"] if q2 else None
                ),
                "source_time_gap_seconds": (
                    (td["td_source_second_ms"] - q2["source_second_ms"]) // 1000
                    if q2
                    else None
                ),
                "fields": {},
            }
            if q2:
                gap = row_evidence["source_time_gap_seconds"]
                source_gap_counts[tag][_source_gap_bucket(gap)] += 1

            for q2_field, td_field in FIELD_MAP.items():
                result = field_reports[tag][q2_field]
                td_value, td_status = td["fields"][td_field]
                q2_value, q2_status = (
                    q2["fields"][q2_field] if q2 else (None, "NO_CANDIDATE")
                )
                if td_status == "NULL":
                    status = "TD_NULL"
                    result["td_null"] += 1
                elif td_status == "INVALID":
                    status = "TD_INVALID"
                    result["td_invalid"] += 1
                elif not q2:
                    status = "NO_Q2_CANDIDATE"
                    result["no_q2_candidate"] += 1
                elif q2_status == "NULL":
                    status = "Q2_NULL"
                    result["q2_null"] += 1
                elif q2_status == "INVALID":
                    status = "Q2_INVALID"
                    result["q2_invalid"] += 1
                elif td_value == q2_value:
                    status = "EQUAL"
                    result["equal"] += 1
                else:
                    status = "DIFFERENT"
                    difference = abs(td_value - q2_value)
                    result["different"] += 1
                    result["absolute_difference_sum"] += difference
                    result["absolute_difference_max"] = max(
                        result["absolute_difference_max"], difference
                    )
                    if len(result["mismatch_samples"]) < 10:
                        result["mismatch_samples"].append(
                            {
                                "symbol": symbol,
                                "td_value": td_value,
                                "q2_value": q2_value,
                                "difference": q2_value - td_value,
                            }
                        )
                if td_status == "PRESENT" and q2_status == "PRESENT":
                    # Aggregate only pairwise comparable values. Null and
                    # invalid values stay visible in their own counters and
                    # are never silently coerced to zero.
                    result["compared_count"] += 1
                    result["td_value_sum"] += td_value
                    result["q2_value_sum"] += q2_value
                    result["signed_difference_sum"] += q2_value - td_value
                if q2:
                    bucket_stats = result["by_source_time_gap_seconds"][
                        _source_gap_bucket(row_evidence["source_time_gap_seconds"])
                    ]
                    if status in {"EQUAL", "DIFFERENT"}:
                        bucket_stats[status.lower()] += 1
                    elif status in {"TD_NULL", "TD_INVALID", "Q2_NULL", "Q2_INVALID"}:
                        bucket_stats[status.lower()] += 1
                row_evidence["fields"][q2_field] = {
                    "td_value": td_value,
                    "td_status": td_status,
                    "q2_value": q2_value,
                    "q2_status": q2_status,
                    "comparison": status,
                }
            symbol_evidence.append(row_evidence)

    tag_reports: dict[str, Any] = {}
    for tag in ANCHOR_TAGS:
        td_timestamps_ms = [
            row["td_timestamp_ms"] for row in td_by_tag[tag].values()
        ]
        td_source_seconds_ms = {
            timestamp_ms // 1000 * 1000 for timestamp_ms in td_timestamps_ms
        }
        tag_reports[tag] = {
            "td_rows": len(td_by_tag[tag]),
            "td_source_timestamp_range_ms": {
                "min": min(td_timestamps_ms) if td_timestamps_ms else None,
                "max": max(td_timestamps_ms) if td_timestamps_ms else None,
                "unique_timestamp_count": len(set(td_timestamps_ms)),
                "unique_source_second_count": len(td_source_seconds_ms),
            },
            "q2_source_time_candidate_rows": len(latest[tag]),
            "q2_candidate_missing_rows": len(td_by_tag[tag]) - len(latest[tag]),
            "source_time_gap_seconds_distribution": source_gap_counts[tag],
            "fields": field_reports[tag],
            "scope": "observed TD auction_snapshot_v2 cohort; not full-market claim",
        }

    summary = {
        "status": "OBSERVED_DIAGNOSTIC_NOT_A_GATE",
        "trade_date": expected_date,
        "timezone": "Asia/Shanghai",
        "time_policy": (
            "candidate alignment only: truncate TD and Q2 source timestamps to "
            "whole seconds; choose the greatest Q2 source second no later than "
            "the TD row source second; same-second ties use later artifact order"
        ),
        "candidate_interval": "[09:15:00, TD row source second]",
        "field_map": FIELD_MAP,
        "q2_frame_order_semantics": "deterministic event-time artifact order; not Rabbit arrival order",
        "historical_available_at": "UNKNOWN",
        "live_visibility": "UNKNOWN",
        "diagnostics": diagnostics,
        "tags": tag_reports,
    }
    return summary, symbol_evidence


def _write_once(path: Path, content: str) -> None:
    with path.open("x", encoding="utf-8") as handle:
        handle.write(content)


def run_audit(
    *,
    td_rows_path: Path,
    q2frame_path: Path,
    output_dir: Path,
    trade_date: str,
) -> dict[str, Any]:
    if output_dir.exists():
        raise FileExistsError(f"output directory already exists: {output_dir}")
    if not td_rows_path.is_file() or not q2frame_path.is_file():
        raise FileNotFoundError("both pinned input files must exist")
    output_dir.mkdir(parents=True, exist_ok=False)

    summary, evidence = compare_td_anchor_fields_to_q2frame(
        _iter_jsonl(td_rows_path),
        _iter_jsonl(q2frame_path),
        trade_date=trade_date,
    )
    summary["inputs"] = {
        "td_rows": {"path": str(td_rows_path), "sha256": _sha256(td_rows_path)},
        "q2frame": {"path": str(q2frame_path), "sha256": _sha256(q2frame_path)},
    }
    summary["side_effects"] = "NONE_OBSERVED; sealed files only"
    summary_path = output_dir / "field_alignment_summary.json"
    evidence_path = output_dir / "symbol_field_evidence.jsonl"
    _write_once(summary_path, json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    _write_once(
        evidence_path,
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in evidence),
    )
    sums_path = output_dir / "sha256sums.txt"
    _write_once(
        sums_path,
        "".join(
            f"{_sha256(path)}  {path.name}\n"
            for path in (summary_path, evidence_path)
        ),
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", default="2026-09-30")
    parser.add_argument("--td-rows", type=Path, default=DEFAULT_TD_ROWS)
    parser.add_argument("--q2frame", type=Path, default=DEFAULT_Q2FRAME)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    summary = run_audit(
        td_rows_path=args.td_rows,
        q2frame_path=args.q2frame,
        output_dir=args.output_dir,
        trade_date=args.trade_date,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
