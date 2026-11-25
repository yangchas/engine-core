"""Compare captured TD auction prices with Q2Frame anchor candidates.

This is a file-only diagnostic over pinned historical evidence. It does not
connect to TDengine, Redis, RabbitMQ, or any production service, and its output
is deliberately not a parity gate.
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
ANCHOR_WINDOWS = {
    # These inclusive whole-second windows reproduce the inspected t1-v2
    # AuctionCalculator source contract. The producing binary is not attested.
    "0920": (time(9, 20, 0), time(9, 20, 20), "a20"),
    "0924": (time(9, 24, 0), time(9, 24, 20), "a24"),
    "0925": (time(9, 25, 0), time(9, 25, 20), "a25"),
}
DEFAULT_Q2FRAME = Path(
    "/home/exedev/validation/task008-opening-q2frame-20260929-20260930T004859+0800/"
    "deployed_release_q2frame_to_0932.jsonl"
)
DEFAULT_TD_ROWS = Path(
    "/home/exedev/validation/task008-opening-plate-amount-integrated-20261001T1142+0800/"
    "td_auction_snapshot_rows.jsonl"
)
DEFAULT_Q2FRAME_SHA256 = "5a2afae406e5a66ea63bb04c10e071de308dbb4bd608e8d4d7a774ea6e9f6bb9"
DEFAULT_TD_ROWS_SHA256 = "b66b78971f6453ad0ab48e5fc818ce7b1e9799afc03bf3e2e9adffae7d44332b"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _compact_date(value: Any) -> str:
    return str(value or "").replace("-", "").strip()


def _parse_td_timestamp_ms(value: Any) -> int | None:
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


def _positive_integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float) and value.is_integer() and value > 0:
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"\+?\d+", value.strip()):
        parsed = int(value)
        return parsed if parsed > 0 else None
    return None


def _td_price(value: Any) -> tuple[int | None, str]:
    if value is None:
        return None, "NULL"
    parsed: int | None = None
    if isinstance(value, int) and not isinstance(value, bool):
        parsed = value
    elif isinstance(value, float) and math.isfinite(value) and value.is_integer():
        parsed = int(value)
    elif isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        parsed = int(value)
    if parsed is None:
        return None, "INVALID"
    if parsed <= 0:
        return None, "NON_POSITIVE"
    return parsed, "PRESENT"


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


def compare_td_anchor_rows_to_q2frame_candidates(
    td_rows: Iterable[Mapping[str, Any]],
    q2_frames: Iterable[Mapping[str, Any]],
    *,
    trade_date: str,
) -> dict[str, Any]:
    """Compare TD snapshot prices with Q2Frame candidate state by whole second.

    The TD row timestamp is truncated to Shanghai whole seconds. For the
    same-second comparison, choose the greatest Q2 source second not later
    than the TD row's second; the last Q2Frame artifact occurrence wins within
    that second. This deterministic replay-artifact order is not Rabbit arrival
    order. The separate window-end value is simply the last matching update in
    Q2Frame artifact order within the source candidate window.
    """

    expected_date = _compact_date(trade_date)
    if not re.fullmatch(r"\d{8}", expected_date):
        raise ValueError("trade_date must be YYYY-MM-DD or YYYYMMDD")

    row_diagnostics = {
        "malformed_rows": 0,
        "unknown_tags": 0,
        "trade_date_mismatch_rows": 0,
        "invalid_symbols": 0,
        "invalid_timestamps": 0,
        "duplicate_tag_symbol_rows": 0,
    }
    td_by_tag: dict[str, dict[str, dict[str, Any]]] = {
        tag: {} for tag in ANCHOR_WINDOWS
    }
    for raw_row in td_rows:
        if not isinstance(raw_row, Mapping):
            row_diagnostics["malformed_rows"] += 1
            continue
        tag = str(raw_row.get("auction_tag") or "").strip()
        if tag not in ANCHOR_WINDOWS:
            row_diagnostics["unknown_tags"] += 1
            continue
        if _compact_date(raw_row.get("trade_date")) != expected_date:
            row_diagnostics["trade_date_mismatch_rows"] += 1
            continue
        symbol = str(raw_row.get("symbol") or "").strip()
        if not re.fullmatch(r"\d{6}", symbol):
            row_diagnostics["invalid_symbols"] += 1
            continue
        td_ts_ms = _parse_td_timestamp_ms(raw_row.get("ts"))
        if td_ts_ms is None:
            row_diagnostics["invalid_timestamps"] += 1
            continue
        if symbol in td_by_tag[tag]:
            row_diagnostics["duplicate_tag_symbol_rows"] += 1
            continue
        td_px, td_price_status = _td_price(raw_row.get("px_milli"))
        td_by_tag[tag][symbol] = {
            "td_ts_ms": td_ts_ms,
            "td_px_milli": td_px,
            "td_price_status": td_price_status,
        }

    q2_diagnostics = {
        "frames": 0,
        "updates": 0,
        "malformed_frames": 0,
        "malformed_updates": 0,
        "invalid_timestamps": 0,
        "invalid_symbols": 0,
        "wrong_trade_date_updates": 0,
        "subsecond_updates": 0,
    }
    # Each event stores the truncated source second and canonical positive
    # candidate (zero/null means unavailable). Lists retain artifact order.
    events: dict[str, dict[str, list[tuple[int, int | None]]]] = {
        tag: defaultdict(list) for tag in ANCHOR_WINDOWS
    }
    for frame in q2_frames:
        q2_diagnostics["frames"] += 1
        if not isinstance(frame, Mapping):
            q2_diagnostics["malformed_frames"] += 1
            continue
        updates = frame.get("q2_updates")
        if not isinstance(updates, list):
            q2_diagnostics["malformed_frames"] += 1
            continue
        for update in updates:
            q2_diagnostics["updates"] += 1
            if not isinstance(update, Mapping):
                q2_diagnostics["malformed_updates"] += 1
                continue
            symbol = str(update.get("symbol") or "").strip()
            if not re.fullmatch(r"\d{6}", symbol):
                q2_diagnostics["invalid_symbols"] += 1
                continue
            timestamp = update.get("ts")
            if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp <= 0:
                q2_diagnostics["invalid_timestamps"] += 1
                continue
            local = datetime.fromtimestamp(timestamp / 1000, SHANGHAI)
            if local.strftime("%Y%m%d") != expected_date:
                q2_diagnostics["wrong_trade_date_updates"] += 1
                continue
            if timestamp % 1000:
                q2_diagnostics["subsecond_updates"] += 1
            local_time = local.time().replace(tzinfo=None)
            for tag, (start, end, field) in ANCHOR_WINDOWS.items():
                if start <= local_time <= end:
                    events[tag][symbol].append(
                        (timestamp // 1000, _positive_integer(update.get(field)))
                    )
                    break

    tag_reports: dict[str, Any] = {}
    for tag, (start, end, field) in ANCHOR_WINDOWS.items():
        rows = td_by_tag[tag]
        same_second_matches = same_second_mismatches = 0
        same_second_unavailable = 0
        window_end_matches = window_end_mismatches = window_end_unavailable = 0
        td_price_seen_anywhere = 0
        td_price_seen_before_snapshot_second = 0
        td_price_seen_in_snapshot_second = 0
        td_price_seen_after_snapshot_second = 0
        q2_positive_same_second_when_td_null = 0
        q2_positive_window_end_when_td_null = 0
        q2_candidate_symbols = 0
        q2_candidate_updates = 0
        candidate_changes = 0
        symbol_evidence: list[dict[str, Any]] = []

        for symbol in sorted(rows):
            td = rows[symbol]
            td_ts_second = td["td_ts_ms"] // 1000
            row_events = events[tag].get(symbol, [])
            if row_events:
                q2_candidate_symbols += 1
                q2_candidate_updates += len(row_events)
            ordered_values: list[int] = []
            seen_values: set[int] = set()
            for _, value in row_events:
                if value is not None and value not in seen_values:
                    seen_values.add(value)
                    ordered_values.append(value)
            if len(ordered_values) > 1:
                candidate_changes += 1

            asof: tuple[int, int | None] | None = None
            for event_second, value in row_events:
                if event_second <= td_ts_second and (
                    asof is None or event_second >= asof[0]
                ):
                    # Equal seconds intentionally let the later artifact item
                    # replace the earlier one; subsecond ordering is discarded.
                    asof = (event_second, value)
            window_end = row_events[-1] if row_events else None
            td_px = td["td_px_milli"]
            same_second_px = asof[1] if asof else None
            window_end_px = window_end[1] if window_end else None

            if td_px is not None:
                if same_second_px is None:
                    same_second_unavailable += 1
                elif same_second_px == td_px:
                    same_second_matches += 1
                else:
                    same_second_mismatches += 1
                if window_end_px is None:
                    window_end_unavailable += 1
                elif window_end_px == td_px:
                    window_end_matches += 1
                else:
                    window_end_mismatches += 1
                matching_candidate_seconds = {
                    event_second
                    for event_second, value in row_events
                    if value == td_px
                }
                if matching_candidate_seconds:
                    td_price_seen_anywhere += 1
                    td_price_seen_before_snapshot_second += any(
                        value_second < td_ts_second
                        for value_second in matching_candidate_seconds
                    )
                    td_price_seen_in_snapshot_second += td_ts_second in matching_candidate_seconds
                    td_price_seen_after_snapshot_second += any(
                        value_second > td_ts_second
                        for value_second in matching_candidate_seconds
                    )
            else:
                if td["td_price_status"] == "NULL":
                    if same_second_px is not None:
                        q2_positive_same_second_when_td_null += 1
                    if window_end_px is not None:
                        q2_positive_window_end_when_td_null += 1

            event_second = asof[0] if asof else None
            symbol_evidence.append(
                {
                    "symbol": symbol,
                    "td_ts_ms": td["td_ts_ms"],
                    "td_whole_second_ms": td_ts_second * 1000,
                    "td_px_milli": td_px,
                    "same_second_q2_candidate_ts_second_ms": (
                        event_second * 1000 if event_second is not None else None
                    ),
                    "same_second_q2_candidate_px_milli": same_second_px,
                    "window_end_q2_candidate_px_milli": window_end_px,
                    "candidate_values_in_window": ordered_values,
                    "td_price_seen_in_window": td_px in seen_values if td_px is not None else None,
                    "td_price_candidate_event_seconds_ms": (
                        sorted(
                            {
                                event_second * 1000
                                for event_second, value in row_events
                                if value == td_px
                            }
                        )
                        if td_px is not None
                        else []
                    ),
                }
            )

        td_positive_count = sum(1 for row in rows.values() if row["td_px_milli"] is not None)
        td_null_count = sum(1 for row in rows.values() if row["td_price_status"] == "NULL")
        td_nonpositive_count = sum(
            1 for row in rows.values() if row["td_price_status"] == "NON_POSITIVE"
        )
        td_invalid_price_count = sum(
            1 for row in rows.values() if row["td_price_status"] == "INVALID"
        )
        diagnostic_status = (
            "OBSERVED_DIFFERENCE_NOT_A_GATE"
            if (
                same_second_mismatches
                or same_second_unavailable
                or q2_positive_same_second_when_td_null
                or window_end_mismatches
                or window_end_unavailable
                or q2_positive_window_end_when_td_null
            )
            else "OBSERVED_ALIGNED_NOT_A_PARITY_GATE"
        )
        tag_reports[tag] = {
            "candidate_window_local_inclusive": [start.isoformat(), end.isoformat()],
            "candidate_field": field,
            "td_rows": len(rows),
            "td_non_null_price_count": td_positive_count,
            "td_null_price_count": td_null_count,
            "td_nonpositive_price_count": td_nonpositive_count,
            "td_invalid_price_count": td_invalid_price_count,
            "q2_candidate_symbols": q2_candidate_symbols,
            "q2_candidate_update_count": q2_candidate_updates,
            "symbols_with_multiple_positive_candidate_values": candidate_changes,
            "same_second_price_match_count": same_second_matches,
            "same_second_price_mismatch_count": same_second_mismatches,
            "same_second_price_unavailable_count": same_second_unavailable,
            "td_price_seen_anywhere_in_window_count": td_price_seen_anywhere,
            "td_price_seen_before_snapshot_second_count": td_price_seen_before_snapshot_second,
            "td_price_seen_in_snapshot_second_count": td_price_seen_in_snapshot_second,
            "td_price_seen_after_snapshot_second_count": td_price_seen_after_snapshot_second,
            "window_end_candidate_match_count": window_end_matches,
            "window_end_candidate_mismatch_count": window_end_mismatches,
            "window_end_candidate_unavailable_count": window_end_unavailable,
            "q2_positive_candidate_when_td_price_null_count": q2_positive_same_second_when_td_null,
            "q2_positive_window_end_candidate_when_td_price_null_count": q2_positive_window_end_when_td_null,
            "diagnostic_status": diagnostic_status,
            "symbol_evidence": symbol_evidence,
        }

    return {
        "contract": "Task008AuctionAnchorCandidateAlignmentV1",
        "trade_date": expected_date,
        "scope": "PINNED_TD_AUCTION_ROWS_VS_T1V2_Q2FRAME_REPLAY_CANDIDATES",
        "comparison_policy": {
            "timezone": "Asia/Shanghai",
            "td_timestamp_use": "stored TD row timestamp truncated to whole seconds",
            "q2_timestamp_use": "Q2 update source timestamp truncated to whole seconds",
            "same_second_tie_break": "last matching Q2Frame artifact occurrence; not Rabbit arrival order",
            "candidate_window": "inclusive whole-second windows from inspected t1-v2 source checkout; producer binary unverified",
            "window_end_value": "last matching update in Q2Frame artifact order within the candidate window",
            "strict_parity_gate": False,
        },
        "td_row_diagnostics": row_diagnostics,
        "q2frame_diagnostics": q2_diagnostics,
        "tags": tag_reports,
        "limits": [
            "This compares captured TD snapshot row values with candidate states in a t1-v2 replay artifact.",
            "The TD row timestamp is not historical available_at or Rabbit arrival time.",
            "Q2Frame artifact order is deterministic replay order, not historical Rabbit delivery/arrival order.",
            "A candidate appearing in the replay window does not prove it was visible at the live freeze.",
            "Observed differences are diagnostic and do not fail the replay or block unrelated Core development.",
        ],
    }


def run_pinned_audit(
    *,
    q2frame_path: Path,
    td_rows_path: Path,
    trade_date: str,
    expected_q2frame_sha256: str,
    expected_td_rows_sha256: str,
) -> dict[str, Any]:
    q2_sha = _sha256(q2frame_path)
    td_sha = _sha256(td_rows_path)
    if q2_sha != expected_q2frame_sha256:
        raise ValueError("Q2Frame SHA-256 does not match the pinned input")
    if td_sha != expected_td_rows_sha256:
        raise ValueError("TD rows SHA-256 does not match the pinned input")
    report = compare_td_anchor_rows_to_q2frame_candidates(
        _iter_jsonl(td_rows_path),
        _iter_jsonl(q2frame_path),
        trade_date=trade_date,
    )
    report["inputs"] = {
        "q2frame_path": str(q2frame_path),
        "q2frame_sha256": q2_sha,
        "td_rows_path": str(td_rows_path),
        "td_rows_sha256": td_sha,
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--q2frame", type=Path, default=DEFAULT_Q2FRAME)
    parser.add_argument("--td-rows", type=Path, default=DEFAULT_TD_ROWS)
    parser.add_argument("--trade-date", default="2026-09-29")
    parser.add_argument("--expected-q2frame-sha256", default=DEFAULT_Q2FRAME_SHA256)
    parser.add_argument("--expected-td-rows-sha256", default=DEFAULT_TD_ROWS_SHA256)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    report = run_pinned_audit(
        q2frame_path=args.q2frame,
        td_rows_path=args.td_rows,
        trade_date=args.trade_date,
        expected_q2frame_sha256=args.expected_q2frame_sha256,
        expected_td_rows_sha256=args.expected_td_rows_sha256,
    )
    args.output_dir.mkdir(parents=True, exist_ok=False)
    report_path = args.output_dir / "anchor_candidate_alignment.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    digest = _sha256(report_path)
    (args.output_dir / "sha256sums.txt").write_text(
        f"{digest}  anchor_candidate_alignment.json\n",
        encoding="utf-8",
    )
    print(json.dumps({"output": str(report_path), "sha256": digest, "tags": {
        tag: {key: value for key, value in values.items() if key != "symbol_evidence"}
        for tag, values in report["tags"].items()
    }}, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
