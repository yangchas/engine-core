#!/usr/bin/env python3
"""Run an explicitly started read-only TD shadow; Redis output is isolated and TTL-bound."""
from __future__ import annotations

import argparse
import os
import shutil
import time
from datetime import datetime, timedelta

from engine_core.live_session import build_live_slice, checkpoint_ready, publish_isolated, states_at


def run_loop(td, redis, *, trade_date, start, end, dry_run=False, auction_interval=30,
             open_interval=60, previous_day_final_yuan=None, sleep_fn=time.sleep,
             now_fn=datetime.now, log_fn=print, relations=None, plate_names=None):
    now, count, prior = now_fn(), 0, None
    while now <= end:
        cutoff = min(max(now, start), end)
        if cutoff.strftime("%H:%M") == "09:45" and not checkpoint_ready(td, cutoff):
            log_fn(f"{cutoff:%Y-%m-%d %H:%M:%S} status=WAITING_FOR_TD_CATCHUP")
            sleep_fn(5)
            now = now_fn()
            continue
        state = states_at(td, cutoff)
        payload = build_live_slice(state, cutoff, previous_day_final_yuan=previous_day_final_yuan,
                                   relations=relations, plate_names=plate_names)
        if not dry_run:
            publish_isolated(redis, trade_date, payload, 172800)
            key_time = cutoff.strftime("%H%M%S")
            publish_isolated(redis, trade_date, payload, 172800, key=f"core:cache:live_tracking:{trade_date}:{key_time}")
        log_fn(f"{cutoff:%Y-%m-%d %H:%M:%S} phase={payload['phase']} status={payload['status']} "
               f"symbols={payload['symbol_count']} lag={payload['data_lag_seconds']}")
        count += 1
        interval = auction_interval if cutoff.strftime("%H:%M:%S") < "09:30:00" else open_interval
        next_cutoff = cutoff + timedelta(seconds=interval)
        if next_cutoff > end: break
        sleep_fn(max(0, (next_cutoff - now_fn()).total_seconds()))
        now = now_fn()
    return count


def _clients():
    import taos
    import redis as redis_module
    td = taos.connect(host=os.getenv("TD_HOST", "127.0.0.1"), port=int(os.getenv("TD_PORT", "6030")),
                      user=os.getenv("TD_USER", "root"), password=os.getenv("TD_PASSWORD", "taosdata"),
                      database=os.getenv("TD_DATABASE", "market_data1"), config=os.getenv("TD_CONFIG", "/etc/taos"))
    red = redis_module.Redis(host=os.getenv("REDIS_HOST", "127.0.0.1"), port=int(os.getenv("REDIS_PORT", "6379")),
                             db=int(os.getenv("REDIS_DB", "0")), password=os.getenv("REDIS_PASSWORD"), decode_responses=True)
    return td, red


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", default=datetime.now().date().isoformat())
    parser.add_argument("--start", default="09:15:20")
    parser.add_argument("--end", default="09:47:00")
    parser.add_argument("--auction-interval", type=int, default=30)
    parser.add_argument("--open-interval", type=int, default=60)
    parser.add_argument("--previous-day-final-yuan", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    start = datetime.fromisoformat(f"{args.trade_date}T{args.start}")
    end = datetime.fromisoformat(f"{args.trade_date}T{args.end}")
    if end < start or args.auction_interval <= 0 or args.open_interval <= 0:
        parser.error("invalid time range or interval")
    if not args.dry_run and shutil.disk_usage(".").used / shutil.disk_usage(".").total > .90:
        parser.error("disk usage exceeds 90%; refusing write mode")
    td, red = _clients()
    try:
        run_loop(td, red, trade_date=args.trade_date, start=start, end=end, dry_run=args.dry_run,
                 auction_interval=args.auction_interval, open_interval=args.open_interval,
                 previous_day_final_yuan=args.previous_day_final_yuan)
    finally:
        close = getattr(td, "close", None)
        if callable(close): close()
        close = getattr(red, "close", None)
        if callable(close): close()


if __name__ == "__main__":
    main()
