"""Helpers for the C577 ladder_daily tests: requests built from the REAL fixture (tests/fixtures/ladder_daily)."""
from __future__ import annotations

import copy
import gzip
import json
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "ladder_daily" / "real_ladder_inputs.json.gz"
_CACHE: dict = {}


def fixture() -> dict:
    if not _CACHE:
        with gzip.open(FIXTURE, "rt", encoding="utf-8") as handle:
            _CACHE.update(json.load(handle))
    return copy.deepcopy(_CACHE)


def request(day: str = "2026-10-27", *, drop_kline=(), drop_pool=(), with_pools=True, extra_days=None, **over) -> dict:
    fx = fixture()
    days = {}
    for d in fx["days"]:
        slot = {}
        k = fx["kline"][d]
        slot["daily_kline"] = ({"status": "unavailable", "reason": "dropped"} if d in drop_kline else
                               {"status": "ok", **k})
        p = fx["pools"].get(d)
        if with_pools:
            slot["kaipan"] = ({"status": "unavailable", "reason": "dropped"} if (d in drop_pool or p is None)
                              else {"status": "ok", **p})
        else:
            slot["kaipan"] = {"status": "unavailable", "reason": "not_loaded"}
        slot["wencai"] = {"status": "unavailable", "reason": "not_configured"}
        days[d] = slot
    for d, slots in (extra_days or {}).items():
        days.setdefault(d, {}).update(slots)
    cal = [d for d in fx["days"] if d <= day]
    req = {"trade_date": day, "as_of": f"{day}T23:00:00+08:00", "fetched_at": f"{day}T23:00:00+08:00",
           "calendar": cal, "source_ladder": ["daily_kline", "kaipan", "wencai"], "window": {"max_days": 20},
           "days": days}
    req.update(over)
    return req


def by_symbol(partition) -> dict:
    return {r["symbol"]: r for r in partition.rows}
