"""Seal all ``auction_snapshot_v2`` rows of one trade date (0920/0924/0925).

TD SELECT only, one bounded query.  Output is a new JSONL file (one object per
row, TD column names); nothing is written to TD or Redis.  It is the input for
offline reconstruction when a session started after the auction.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

COLUMNS = ("ts", "px_milli", "chg_bp", "match_amt_yuan", "rest_bid_amt_yuan",
           "rest_ask_amt_yuan", "limit_state", "symbol", "trade_date", "auction_tag")


def _date(value: str) -> str:
    parsed = datetime.strptime(value, "%Y-%m-%d").date()
    if parsed.isoformat() != value:
        raise ValueError("date must be strict YYYY-MM-DD")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    trade_date = _date(args.trade_date)
    database = os.environ.get("TDENGINE_DATABASE", "market_data1")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", database):
        raise ValueError("invalid database name")
    import taos  # type: ignore[import-not-found]

    sql = (
        "SELECT ts, px_milli, chg_bp, match_amt_yuan, rest_bid_amt_yuan, "
        "rest_ask_amt_yuan, limit_state, symbol, trade_date, auction_tag "
        f"FROM {database}.auction_snapshot_v2 "
        f'WHERE trade_date="{trade_date.replace("-", "")}" '
        'AND auction_tag IN ("0920","0924","0925") ORDER BY ts'
    )
    connection = taos.connect(
        host=os.environ.get("TDENGINE_HOST", "127.0.0.1"),
        port=int(os.environ.get("TDENGINE_PORT", "6030")),
        user=os.environ.get("TDENGINE_USER", "root"),
        password=os.environ.get("TDENGINE_PASSWORD", "taosdata"),
        database=database,
        config=os.environ.get("TDENGINE_CONFIG", "/etc/taos"),
    )
    try:
        cursor = connection.cursor()
        cursor.execute(sql)
        fetched = list(cursor.fetchall())
    finally:
        connection.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    counts: dict = {}
    with args.output.open("x", encoding="utf-8") as handle:
        for row in fetched:
            record = dict(zip(COLUMNS, row))
            record["ts"] = str(record["ts"])
            counts[record["auction_tag"]] = counts.get(record["auction_tag"], 0) + 1
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) + "\n")
    print(json.dumps({"trade_date": trade_date, "rows": len(fetched), "by_tag": counts,
                      "observed_at": datetime.now(timezone.utc).isoformat(),
                      "side_effect_boundary": "TD SELECT only"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
