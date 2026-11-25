"""Audit auction-revision identity against a pinned real t1-v2 Q2Frame.

The source is an event-time replay artifact, not Rabbit arrival history. This
runner compares the 09:25 first-observable anchor with the first post-soft
Q2Frame state and reports whether unrelated later quote updates create a new
auction-anchor content revision.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from engine_core import (  # noqa: E402
    AuctionTimeline,
    Q2FrameV1,
    local_datetime_ms,
    semantic_hash,
)
from engine_core.q2 import normalize_q2  # noqa: E402


SHANGHAI = ZoneInfo("Asia/Shanghai")
ANCHOR_FIELD = "auction_anchor_0925_price_milli"
NON_ANCHOR_FIELDS = (
    "price_milli",
    "pre_close_milli",
    "amount_yuan",
    "volume_lots",
    "instant_volume_lots",
    "instant_amount_yuan",
    "large_net_yuan",
    "amount_2m_yuan",
    "speed_1m_bp",
    "source_record_time_ms",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _frames(path: Path) -> Iterable[Q2FrameV1]:
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, Mapping):
                raise ValueError(f"Q2Frame line {line_no} must be an object")
            yield Q2FrameV1.from_mapping(value)


def _floor_second(timestamp_ms: int) -> int:
    return timestamp_ms - timestamp_ms % 1000


def _local(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc).astimezone(SHANGHAI).isoformat()


def _apply_updates(
    updates: Iterable[Mapping[str, Any]],
    raw_by_symbol: dict[str, dict[str, Any]],
    state: dict[str, Mapping[str, Any]],
) -> None:
    for update in updates:
        symbol = str(update.get("symbol", "")).strip()
        if not symbol:
            continue
        raw = raw_by_symbol.setdefault(symbol, {})
        raw.update({key: value for key, value in update.items() if key != "symbol"})
        quote = normalize_q2(symbol, raw)
        row = dict(quote.to_mapping())
        row["source_time_ms"] = quote.source_record_time_ms
        state[symbol] = row


def _unrelated_state_hash(rows: Mapping[str, Mapping[str, Any]]) -> str:
    return semantic_hash(
        {
            symbol: {field: row.get(field) for field in NON_ANCHOR_FIELDS}
            for symbol, row in rows.items()
        }
    )


def audit(path: Path, trade_date: str) -> dict[str, Any]:
    first_observable_ms = local_datetime_ms(trade_date, "09:25:06")
    soft_deadline_ms = local_datetime_ms(trade_date, "09:25:30")
    post_soft_frame_ms = soft_deadline_ms

    symbols: set[str] = set()
    expected_seq = 1
    previous_frame_ms = 0
    for frame in _frames(path):
        if frame.seq_no != expected_seq:
            raise ValueError("Q2Frame seq_no is not continuous")
        if frame.logical_ts_ms < previous_frame_ms:
            raise ValueError("Q2Frame logical time moved backwards")
        expected_seq += 1
        previous_frame_ms = frame.logical_ts_ms
        symbols.update(
            str(update.get("symbol", "")).strip()
            for update in frame.q2_updates
            if str(update.get("symbol", "")).strip()
        )
    if not symbols:
        raise ValueError("Q2Frame artifact contains no symbols")
    expected_symbols = tuple(sorted(symbols))

    timeline = AuctionTimeline(trade_date)
    raw_by_symbol: dict[str, dict[str, Any]] = {}
    state: dict[str, Mapping[str, Any]] = {}
    first_observation = None
    later_observation = None
    first_cut_seq = None
    first_cut_frame_ms = None
    first_unrelated_hash = None
    first_full_row_hash = None
    post_cut_seq = None
    post_cut_frame_ms = None
    post_full_row_hash = None
    last_processed_seq = None
    last_processed_frame_ms = None
    frame_count = update_count = 0

    for frame in _frames(path):
        frame_second = _floor_second(frame.logical_ts_ms)
        if first_observation is None and frame_second > first_observable_ms:
            first_observation = timeline.observe(
                "0925",
                state,
                evaluation_time_ms=first_observable_ms,
                expected_symbols=expected_symbols,
                source_layers=("t1_v2_q2frame_event_time_replay",),
            )
            first_cut_seq = last_processed_seq
            first_cut_frame_ms = last_processed_frame_ms
            first_unrelated_hash = _unrelated_state_hash(state)
            first_full_row_hash = semantic_hash(state)

        _apply_updates(frame.q2_updates, raw_by_symbol, state)
        frame_count += 1
        update_count += len(frame.q2_updates)

        if first_observation is None and frame_second == first_observable_ms:
            first_observation = timeline.observe(
                "0925",
                state,
                evaluation_time_ms=first_observable_ms,
                expected_symbols=expected_symbols,
                source_layers=("t1_v2_q2frame_event_time_replay",),
            )
            first_cut_seq = frame.seq_no
            first_cut_frame_ms = frame.logical_ts_ms
            first_unrelated_hash = _unrelated_state_hash(state)
            first_full_row_hash = semantic_hash(state)

        if later_observation is None and frame_second >= post_soft_frame_ms:
            later_observation = timeline.observe(
                "0925",
                state,
                evaluation_time_ms=frame_second,
                expected_symbols=expected_symbols,
                source_layers=("t1_v2_q2frame_event_time_replay",),
            )
            post_cut_seq = frame.seq_no
            post_cut_frame_ms = frame.logical_ts_ms
            post_full_row_hash = semantic_hash(state)
            break

        last_processed_seq = frame.seq_no
        last_processed_frame_ms = frame.logical_ts_ms

    if first_observation is None:
        first_observation = timeline.observe(
            "0925",
            state,
            evaluation_time_ms=first_observable_ms,
            expected_symbols=expected_symbols,
            source_layers=("t1_v2_q2frame_event_time_replay",),
        )
        first_cut_seq = frame_count
        first_cut_frame_ms = last_processed_frame_ms
        first_unrelated_hash = _unrelated_state_hash(state)
        first_full_row_hash = semantic_hash(state)
    if later_observation is None:
        raise ValueError("artifact has no Q2Frame at or after the 09:25:30 soft deadline")

    first_anchor_hash = first_observation.observations_hash
    later_anchor_hash = later_observation.observations_hash
    later_unrelated_hash = _unrelated_state_hash(state)
    quality_counts = Counter(
        str(
            row.get("auction_anchor_field_quality", {}).get("a25", "UNKNOWN")
            if isinstance(row.get("auction_anchor_field_quality"), Mapping)
            else "UNKNOWN"
        )
        for row in state.values()
    )
    return {
        "schema": "Task008AuctionRevisionScopeAuditV1",
        "trade_date": trade_date,
        "source": {
            "path": str(path),
            "sha256": _sha256(path),
            "kind": "pinned_t1_v2_q2frame_event_time_replay",
            "rabbit_arrival_order": "UNKNOWN",
            "historical_available_at": "UNKNOWN",
        },
        "first_observable": {
            "evaluation_time_ms": first_observable_ms,
            "evaluation_time_local": _local(first_observable_ms),
            "last_frame_seq_included": first_cut_seq,
            "last_frame_time_ms_included": first_cut_frame_ms,
            "last_frame_time_local_included": (
                _local(first_cut_frame_ms) if first_cut_frame_ms else None
            ),
            "revision": first_observation.revision,
            "state": first_observation.state,
            "content_hash": first_observation.content_hash,
            "anchor_observations_hash": first_anchor_hash,
            "source_time_max_ms": first_observation.source_time_max_ms,
            "source_observed_count": len(first_observation.source_observed_symbols),
            "anchor_available_count": len(first_observation.available_anchor_symbols),
            "anchor_missing_count": len(first_observation.missing_anchor_symbols),
        },
        "post_soft_observation": {
            "policy_soft_deadline_ms": soft_deadline_ms,
            "evaluation_time_ms": later_observation.evaluation_time_ms,
            "evaluation_time_local": _local(later_observation.evaluation_time_ms),
            "frame_seq_included": post_cut_seq,
            "frame_time_ms_included": post_cut_frame_ms,
            "frame_time_local_included": _local(post_cut_frame_ms),
            "revision": later_observation.revision,
            "state": later_observation.state,
            "content_hash": later_observation.content_hash,
            "anchor_observations_hash": later_anchor_hash,
            "source_time_max_ms": later_observation.source_time_max_ms,
            "source_observed_count": len(later_observation.source_observed_symbols),
            "anchor_available_count": len(later_observation.available_anchor_symbols),
            "anchor_missing_count": len(later_observation.missing_anchor_symbols),
            "full_q2_row_hash": post_full_row_hash,
            "delay_after_soft_deadline_ms": post_cut_frame_ms - soft_deadline_ms,
            "anchor_quality_counts": dict(sorted(quality_counts.items())),
        },
        "comparison": {
            "same_anchor_content": first_anchor_hash == later_anchor_hash,
            "same_revision": first_observation.revision == later_observation.revision,
            "same_content_hash": first_observation.content_hash == later_observation.content_hash,
            "timeline_history_length": len(timeline.revisions("0925")),
            "same_unrelated_q2_state": first_unrelated_hash == later_unrelated_hash,
            "first_unrelated_q2_state_hash": first_unrelated_hash,
            "post_soft_unrelated_q2_state_hash": later_unrelated_hash,
            "same_full_q2_row_state": first_full_row_hash == post_full_row_hash,
            "first_full_q2_row_hash": first_full_row_hash,
            "post_soft_full_q2_row_hash": post_full_row_hash,
            "content_hash_contract": "anchor value+quality only; source/evaluation metadata is evidence",
        },
        "inventory": {
            "expected_symbol_count_from_whole_artifact": len(expected_symbols),
            "frames_consumed_through_post_soft_frame": frame_count,
            "updates_consumed_through_post_soft_frame": update_count,
        },
        "limitations": [
            "The later frame is event-time replay, not proof of Rabbit late arrival or historical Redis availability.",
            "This audit tests revision identity against the pinned Q2Frame cohort; it does not claim NORMAL opening acceptance.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--q2frame", type=Path, required=True)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.q2frame, args.trade_date)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "source_sha256": result["source"]["sha256"],
                "same_anchor_content": result["comparison"]["same_anchor_content"],
                "same_revision": result["comparison"]["same_revision"],
                "revision_count": result["comparison"]["timeline_history_length"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
