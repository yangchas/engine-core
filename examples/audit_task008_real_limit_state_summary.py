"""Audit Core limit-state aggregation against pinned real Q2Frame evidence.

This reads a saved Core report produced from a real exact-release t1-v2 replay
and the matching producer command journal.  It does not connect to Redis,
TDengine, RabbitMQ, or production services, and it does not repeat the replay.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from engine_core import (  # noqa: E402
    build_opening_limit_state_summary,
    canonical_json,
)


PINNED_DIR = Path(
    "/home/exedev/validation/task008-opening-q2frame-20260929-20260930T004859+0800"
)
PINNED_CORE_REPORT_SHA256 = (
    "6fdc8485e9877b9f136148b594b1c77cac963868ab5fc27818d68bb2c11f180c"
)
PINNED_Q2FRAME_SHA256 = (
    "5a2afae406e5a66ea63bb04c10e071de308dbb4bd608e8d4d7a774ea6e9f6bb9"
)
PINNED_PRODUCER_JOURNAL_SHA256 = (
    "3422c51fe911172cef247a0eb23ad98f7987a34325abc83db943bebf76f5993f"
)
SHANGHAI = ZoneInfo("Asia/Shanghai")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_jsonl_record(path: Path, *, key: str) -> Mapping[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if isinstance(row, Mapping) and row.get("key") == key:
                return row
    raise ValueError(f"producer journal has no command for key {key}")


def _known_limit_state_counts(rows: Mapping[str, Mapping[str, Any]]) -> dict[str, int]:
    counts = {"up_count": 0, "normal_count": 0, "down_count": 0}
    for row in rows.values():
        value = row.get("limit_state")
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        if value == 1:
            counts["up_count"] += 1
        elif value == 0:
            counts["normal_count"] += 1
        elif value == -1:
            counts["down_count"] += 1
    return counts


def _limit_state_quality(rows: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    present = sum(row.get("limit_state") is not None for row in rows.values())
    valid = sum(
        not isinstance(row.get("limit_state"), bool)
        and isinstance(row.get("limit_state"), int)
        and row.get("limit_state") in {-1, 0, 1}
        for row in rows.values()
    )
    invalid = present - valid
    total = len(rows)
    status = (
        "available"
        if total and valid == total
        else "partial"
        if valid
        else "unavailable"
    )
    return {
        "present_count": present,
        "valid_count": valid,
        "invalid_count": invalid,
        "missing_count": total - present,
        "status": status,
    }


def audit(
    *,
    core_report_path: Path,
    q2frame_path: Path,
    producer_journal_path: Path,
    expected_core_report_sha256: str,
    expected_q2frame_sha256: str,
    expected_producer_journal_sha256: str,
) -> dict[str, Any]:
    observed_hashes = {
        "core_report": _sha256(core_report_path),
        "q2frame": _sha256(q2frame_path),
        "producer_journal": _sha256(producer_journal_path),
    }
    expected_hashes = {
        "core_report": expected_core_report_sha256,
        "q2frame": expected_q2frame_sha256,
        "producer_journal": expected_producer_journal_sha256,
    }
    if observed_hashes != expected_hashes:
        raise ValueError(
            "input SHA-256 differs from the pinned evidence; pass reviewed hashes explicitly"
        )

    report = json.loads(core_report_path.read_text(encoding="utf-8"))
    if report.get("q2frame", {}).get("sha256") != observed_hashes["q2frame"]:
        raise ValueError("Core report does not identify the supplied Q2Frame artifact")
    opening = report["ordered"]["opening_evidence"]["OPENING_0932"]
    facts = opening["facts_by_symbol"]
    expected_symbols = tuple(report["inventory"]["symbols"])
    trade_date = str(report["trade_date"])
    evaluation_ms = int(opening["evaluation_time_ms"])
    freshness = opening["freshness_policy"]
    stale_after_ms = int(freshness["stale_after_ms"])
    max_future_skew_ms = int(freshness["max_future_skew_ms"])

    fresh: dict[str, Mapping[str, Any]] = {}
    stale: dict[str, Mapping[str, Any]] = {}
    unclassified: dict[str, Mapping[str, Any]] = {}
    for symbol, fact in facts.items():
        timestamp_ms = fact.get("timestamp_ms")
        if isinstance(timestamp_ms, bool) or not isinstance(timestamp_ms, int):
            unclassified[symbol] = fact
            continue
        local_date = datetime.fromtimestamp(
            timestamp_ms / 1000, timezone.utc
        ).astimezone(SHANGHAI).date().isoformat()
        age_ms = evaluation_ms - timestamp_ms
        if local_date != trade_date or timestamp_ms > evaluation_ms + max_future_skew_ms:
            unclassified[symbol] = fact
        elif age_ms > stale_after_ms:
            stale[symbol] = fact
        else:
            fresh[symbol] = fact

    observed_summary = build_opening_limit_state_summary(
        facts,
        expected_symbols=expected_symbols,
        scope="OBSERVED_COHORT",
    )
    fresh_summary = build_opening_limit_state_summary(
        fresh,
        expected_symbols=tuple(fresh),
        scope="FRESH_OBSERVED_COHORT",
    )
    stale_summary = build_opening_limit_state_summary(
        stale,
        expected_symbols=tuple(stale),
        scope="STALE_OBSERVED_COHORT",
    )
    unclassified_summary = build_opening_limit_state_summary(
        unclassified,
        expected_symbols=tuple(unclassified),
        scope="UNCLASSIFIED_TIME_OBSERVED_COHORT",
    )

    producer_key = f"market:opening:cutoff:{trade_date.replace('-', '')}:0932:payload"
    command = _read_jsonl_record(producer_journal_path, key=producer_key)
    producer_rows_list = json.loads(command["payload"]).get("rows", [])
    producer_rows = {str(row["symbol"]): row for row in producer_rows_list}
    if len(producer_rows) != len(producer_rows_list):
        raise ValueError("producer payload contains duplicate symbols")
    producer_fields = dict(command.get("fields") or {})
    producer_counts = _known_limit_state_counts(producer_rows)
    producer_quality = _limit_state_quality(producer_rows)
    core_fresh_state = {
        symbol: fact.get("limit_state") for symbol, fact in fresh.items()
    }
    producer_state = {
        symbol: row.get("limit_state") for symbol, row in producer_rows.items()
    }
    membership_equal = set(fresh) == set(producer_rows)
    state_mismatches = sum(
        1
        for symbol in set(fresh) & set(producer_rows)
        if core_fresh_state[symbol] != producer_state[symbol]
    )
    summary_matches_producer = (
        fresh_summary["limit_state_present_count"]
        == int(producer_fields["limit_state_present_count"])
        and fresh_summary["limit_state_valid_count"]
        == int(producer_fields["limit_state_valid_count"])
        and fresh_summary["limit_state_invalid_count"]
        == int(producer_fields["limit_state_invalid_count"])
        and fresh_summary["cohort_field_status"] == producer_quality["status"]
        and fresh_summary["limit_state_counts"] == producer_counts
        and producer_quality["present_count"]
        == int(producer_fields["limit_state_present_count"])
        and producer_quality["valid_count"]
        == int(producer_fields["limit_state_valid_count"])
        and producer_quality["invalid_count"]
        == int(producer_fields["limit_state_invalid_count"])
    )
    checks = {
        "core_report_q2frame_identity": True,
        "fresh_membership_matches_producer": membership_equal,
        "fresh_limit_state_per_symbol_mismatches_zero": state_mismatches == 0,
        "fresh_limit_state_summary_matches_producer": summary_matches_producer,
    }
    return {
        "status": "PASS_WITH_LIMITS" if all(checks.values()) else "MISMATCH",
        "trade_date": trade_date,
        "evaluation_time_ms": evaluation_ms,
        "input_hashes": observed_hashes,
        "core_source": "saved Core opening facts from exact-release TD-to-t1-v2 Q2Frame replay",
        "producer_source": "same-run t1-v2 opening_cutoff_v1 command journal",
        "checks": checks,
        "cohorts": {
            "observed": observed_summary,
            "fresh": fresh_summary,
            "stale": stale_summary,
            "time_unclassified": unclassified_summary,
        },
        "producer": {
            "row_count": len(producer_rows),
            "limit_state_present_count": int(
                producer_fields["limit_state_present_count"]
            ),
            "limit_state_valid_count": int(
                producer_fields["limit_state_valid_count"]
            ),
            "limit_state_invalid_count": int(
                producer_fields["limit_state_invalid_count"]
            ),
            "limit_state_missing_count": producer_quality["missing_count"],
            "cohort_field_status": producer_quality["status"],
            "limit_state_counts": producer_counts,
            "snapshot_integrity_status": producer_fields.get(
                "snapshot_integrity_status"
            ),
            "universe_authority_status": producer_fields.get(
                "universe_authority_status"
            ),
            "universe_asof_ts": producer_fields.get("universe_asof_ts"),
        },
        "limits": [
            "This validates aggregation over a saved real Core report; it does not repeat the TD replay.",
            "Producer and Core outputs are from the same t1-v2 replay, not independent live Redis evidence.",
            "Universe authority is partial; observed counts are not full-market counts.",
            "The result does not prove Rabbit delivery order or historical available_at.",
        ],
        "production_side_effects": "NONE; local frozen artifacts only",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-report", type=Path, default=PINNED_DIR / "core_auction_opening_shadow.json")
    parser.add_argument("--q2frame", type=Path, default=PINNED_DIR / "deployed_release_q2frame_to_0932.jsonl")
    parser.add_argument("--producer-journal", type=Path, default=PINNED_DIR / "deployed_release_auction_commands_to_0932.jsonl")
    parser.add_argument("--core-report-sha256", default=PINNED_CORE_REPORT_SHA256)
    parser.add_argument("--q2frame-sha256", default=PINNED_Q2FRAME_SHA256)
    parser.add_argument("--producer-journal-sha256", default=PINNED_PRODUCER_JOURNAL_SHA256)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    result = audit(
        core_report_path=args.core_report,
        q2frame_path=args.q2frame,
        producer_journal_path=args.producer_journal,
        expected_core_report_sha256=args.core_report_sha256,
        expected_q2frame_sha256=args.q2frame_sha256,
        expected_producer_journal_sha256=args.producer_journal_sha256,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        handle.write(canonical_json(result))
        handle.write("\n")
    print(canonical_json({"output": str(args.output), "status": result["status"], "checks": result["checks"]}))
    return 0 if result["status"] == "PASS_WITH_LIMITS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
