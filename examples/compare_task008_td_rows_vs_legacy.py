"""Compare TD ``auction_snapshot_v2`` aggregates with the legacy tag summaries.

Local files only: the JSONL from ``run_real_td_auction_rows.py`` and the output
of ``run_real_redis_auction_projection.py``.  Nothing is repaired.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from engine_core import derive_td_auction_summary  # noqa: E402
from examples.compare_task008_tag_summaries_vs_legacy import FIELD_MAP  # noqa: E402


def compare(td_rows: list, legacy_projection: dict) -> dict:
    out = {}
    for item in legacy_projection.get("projections", ()):
        tag, summary = item.get("tag"), item.get("summary") or {}
        if tag not in ("0920", "0924", "0925") or not summary:
            continue
        core = derive_td_auction_summary(td_rows, tag=tag)
        diffs = {
            lk: {"legacy": summary.get(lk), "td": core.get(ck)}
            for lk, ck in FIELD_MAP.items() if summary.get(lk) != core.get(ck)
        }
        out[tag] = {"status": "ALL_EQUAL" if not diffs else "DIFFERENT",
                    "fields_equal": len(FIELD_MAP) - len(diffs), "fields_compared": len(FIELD_MAP),
                    "differences": diffs}
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--td-rows", type=Path, required=True)
    parser.add_argument("--legacy-projection", type=Path, required=True)
    args = parser.parse_args()
    rows = [json.loads(l) for l in args.td_rows.read_text(encoding="utf-8").splitlines() if l.strip()]
    legacy = json.loads(args.legacy_projection.read_text(encoding="utf-8"))
    print(json.dumps(compare(rows, legacy), ensure_ascii=False, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
