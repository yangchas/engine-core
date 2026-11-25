"""Thin Redis/TD replayers over recorded raw upstream exports (examples/record_quote_slice_fixture.py, C662c)."""
from __future__ import annotations

import gzip
import json
from pathlib import Path

V0 = Path(__file__).parent / "fixtures" / "real" / "c662"      # recorded 2026-11-03, see PROVENANCE `_c662_recordings`
DAY = "20261103"


def _read(name):
    with gzip.open(V0 / name, "rt", encoding="utf-8") as handle:
        return json.load(handle)


class ReplayPipeline:
    def __init__(self, redis):
        self.redis = redis
        self.commands = []

    def hgetall(self, key):
        self.commands.append(("hgetall", key))
        return self

    def hget(self, key, field):
        self.commands.append(("hget", key, field))
        return self

    def execute(self):
        results = []
        for command in self.commands:
            if command[0] == "hgetall":
                results.append(self.redis.hashes.get(command[1], {}))
            else:
                value = self.redis.hashes.get(command[1], {}).get(command[2])
                results.append(value)
        return results


class ReplayRedis:
    def __init__(self, auction, q2):
        self.hashes = {key: value["value"] for key, value in auction.items()
                       if value.get("type") == "hash"}
        self.sets = {key: value["value"] for key, value in auction.items()
                     if value.get("type") == "set"}
        self.hashes.update({f"q2:{symbol}": item["value"] for symbol, item in q2.items()})

    def smembers(self, key):
        return self.sets.get(key, [])

    def pipeline(self, transaction=False):
        assert transaction is False
        return ReplayPipeline(self)


class ReplayTD:
    def __init__(self, capture):
        self.capture = capture
        self.columns = capture["cols"]
        self.rows = capture["rows"]
        self.query = ""
        self.selected = []

    def cursor(self):
        return self

    def execute(self, query):
        import re
        self.query = query
        day, tag = re.search(r"trade_date='(\d+)' and auction_tag='(\d+)'", query).groups()
        day_i, tag_i = self.columns.index("trade_date"), self.columns.index("auction_tag")
        fields = [self.columns.index(name) for name in (
            "ts", "px_milli", "chg_bp", "match_amt_yuan", "rest_bid_amt_yuan",
            "rest_ask_amt_yuan", "limit_state", "symbol", "trade_date", "auction_tag")]
        self.selected = [tuple(row[index] for index in fields) for row in self.rows
                         if row[day_i] == day and row[tag_i] == tag]

    def fetchall(self):
        return list(self.selected)

    def close(self):
        pass


def load_v0():
    return (_read(f"redis_auction_keys_{DAY}.json.gz"),
            _read(f"redis_q2_hashes_{DAY}.json.gz"),
            _read(f"td_auction_snapshot_v2_{DAY}.json.gz"))


def replay_clients(raw):
    auction, q2, td = raw
    return ReplayRedis(auction, q2), ReplayTD(td)


EXCERPT = Path(__file__).parent / "fixtures" / "real" / "c526" / "redis_q2_hashes_20261023_first60.json.gz"


def q2_excerpt_redis():
    """2026-10-23 terminal q2 hashes of the first 60 symbols (recorded excerpt, PROVENANCE `_c526_excerpts`).
    The active set is the excerpt's own symbols: nothing is invented, the day is simply restricted to them."""
    with gzip.open(EXCERPT, "rt", encoding="utf-8") as handle:
        q2 = json.load(handle)
    return ReplayRedis({"q2:active:20261023": {"type": "set", "value": sorted(q2)}}, q2), sorted(q2)
