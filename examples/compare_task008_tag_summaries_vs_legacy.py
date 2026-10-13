"""Compare Core per-tag Q2 summaries with the legacy ``market:auction:{date}:{tag}`` summaries.

Inputs are local files only: ``tag_summaries.jsonl`` written by the shadow
script (one record per poll, ``source_time_max`` in epoch ms) and the output
of ``run_real_redis_auction_projection.py``.  The legacy summary is frozen at
its own ``ts``; a Core poll is only comparable when its Q2 state is from about
that instant, so for each tag the poll with the nearest ``source_time_max`` is
chosen and the time gap is reported.  Nothing is repaired or interpreted.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

# legacy summary key -> Core metric
FIELD_MAP = {
    "total_stocks": "stock_count",
    "valid_stock_count": "valid_stock_count",
    "unavailable_stock_count": "unavailable_stock_count",
    "high_open_count": "positive_count",
    "low_open_count": "negative_count",
    "flat_open_count": "flat_count",
    "total_auction_amount_yuan": "auction_amount_yuan",
    "limit_up_count": "limit_up_count",
    "limit_down_count": "limit_down_count",
    "total_limit_up_bid_amount_yuan": "limit_up_seal_amount_yuan",
}


def _legacy_by_tag(projection: Mapping[str, Any]) -> dict:
    out = {}
    for item in projection.get("projections", ()):
        summary = item.get("summary") or {}
        if item.get("tag") and summary:
            out[item["tag"]] = summary
    return out


def compare(polls: list, legacy_projection: Mapping[str, Any], *, max_gap_ms: int = 6000) -> dict:
    result = {}
    for tag, summary in sorted(_legacy_by_tag(legacy_projection).items()):
        legacy_ts = summary.get("ts")
        candidates = [
            p for p in polls
            if isinstance(p.get("source_time_max"), int) and tag in p
        ]
        if not candidates or not isinstance(legacy_ts, int):
            result[tag] = {"status": "NO_COMPARABLE_POLL", "legacy_ts": legacy_ts}
            continue
        best = min(candidates, key=lambda p: abs(p["source_time_max"] - legacy_ts))
        core = best[tag]
        diffs = {}
        equal = 0
        for legacy_key, core_key in FIELD_MAP.items():
            a, b = summary.get(legacy_key), core.get(core_key)
            if a == b:
                equal += 1
            else:
                diffs[legacy_key] = {"legacy": a, "core": b,
                                     "delta": (b - a) if isinstance(a, int) and isinstance(b, int) else None}
        result[tag] = {
            "status": (
                "NOT_TIME_ALIGNED" if abs(best["source_time_max"] - legacy_ts) > max_gap_ms
                else "ALL_EQUAL" if not diffs else "DIFFERENT"
            ),
            "legacy_ts": legacy_ts,
            "core_poll": best.get("poll"),
            "core_source_time_max": best["source_time_max"],
            "time_gap_ms": best["source_time_max"] - legacy_ts,
            "fields_equal": equal,
            "fields_compared": len(FIELD_MAP),
            "differences": diffs,
        }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag-summaries", type=Path, required=True, nargs="+",
                        help="one or more tag_summaries.jsonl files (polls are merged)")
    parser.add_argument("--legacy-projection", type=Path, required=True)
    parser.add_argument("--max-gap-ms", type=int, default=6000,
                        help="nearest poll further than this from the legacy ts is NOT_TIME_ALIGNED")
    args = parser.parse_args()
    polls = []
    for path in args.tag_summaries:
        polls.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    legacy = json.loads(args.legacy_projection.read_text(encoding="utf-8"))
    print(json.dumps(compare(polls, legacy, max_gap_ms=args.max_gap_ms), ensure_ascii=False, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
