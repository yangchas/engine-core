import json
from pathlib import Path

import pytest

from engine_core.auction_node import run_auction_node
from engine_core.clock import local_datetime_ms


class StrictAuctionClient:
    def __init__(self):
        self.calls = []

    def smembers(self, key):
        self.calls.append("smembers")
        return {"q2:active:20261013": ["000001", "000002"]}.get(key, [])

    def hgetall(self, key):
        self.calls.append("hgetall")
        symbol = key.removeprefix("q2:")
        rows = {
            "000001": {"px": "1000", "pc": "990", "amt": "100", "ts": "1791854700000", "a20": "990", "a24": "995", "a25": "1000"},
            "000002": {"px": "2000", "pc": "1990", "amt": "200", "ts": "1791854700000", "a20": "1990", "a24": "1995", "a25": "2000"},
        }
        return rows.get(symbol, {})

    def type(self, key):
        self.calls.append("type")
        return b"none"

    def hlen(self, key):
        self.calls.append("hlen")
        return 0

    def hscan_iter(self, key):
        self.calls.append("hscan_iter")
        return iter(())

    def get(self, key):
        self.calls.append("get")
        return None

    def ttl(self, key):
        self.calls.append("ttl")
        return -2

    def close(self):
        pass

    def __getattr__(self, name):
        raise AssertionError("unexpected client operation: " + name)


def _node(tmp_path: Path, client, ledger, times, sleep_until):
    return run_auction_node(
        trade_date="2026-10-13", out_dir=tmp_path / "node", live_client=client,
        ledger_path=ledger, read_times=times, confirm_read_only=True,
        sleep_until=sleep_until,
    )


def test_auction_node_dispatches_timers_and_restores_them_on_restart(tmp_path):
    client = StrictAuctionClient()
    ledger = tmp_path / "ledger.jsonl"
    times = ("09:25:12", "09:26:05", "09:32:05")
    clock = lambda date, hms: local_datetime_ms(date, hms)  # noqa: E731
    first = _node(tmp_path, client, ledger, times, clock)
    assert first["timers_acknowledged"] == ["AUCTION_0926", "OPENING_0932"]
    for timer_id in first["timers_acknowledged"]:
        row = json.loads((tmp_path / "node" / f"timer_{timer_id}.json").read_text())
        assert row["origin"] == "NORMAL"
        assert row["anchor_table_sha256"]
        assert (tmp_path / "node" / row["anchor_table_file"]).is_file()
    ledger_lines = ledger.read_text().splitlines()
    second = _node(tmp_path, client, ledger, times, clock)
    assert second["timers_acknowledged"] == []
    assert second["timers_restored_from_ledger"] == ["AUCTION_0926", "OPENING_0932"]
    assert ledger.read_text().splitlines() == ledger_lines
    assert set(client.calls) <= {"smembers", "hgetall", "type", "hlen", "hscan_iter", "get", "ttl"}


def test_late_first_poll_marks_both_timers_as_recovery_catchup(tmp_path):
    client = StrictAuctionClient()
    late_times = ("09:33:05",)
    clock = lambda date, hms: local_datetime_ms(date, hms)  # noqa: E731
    result = _node(tmp_path, client, tmp_path / "ledger.jsonl", late_times, clock)
    assert result["timers_acknowledged"] == ["AUCTION_0926", "OPENING_0932"]
    assert all(
        json.loads((tmp_path / "node" / f"timer_{timer_id}.json").read_text())["origin"]
        == "RECOVERY_CATCHUP"
        for timer_id in result["timers_acknowledged"]
    )


def test_auction_node_requires_read_only_confirmation(tmp_path):
    with pytest.raises(RuntimeError, match="confirm_read_only"):
        run_auction_node(
            trade_date="2026-10-13", out_dir=tmp_path / "node",
            live_client=StrictAuctionClient(), ledger_path=tmp_path / "ledger.jsonl",
            read_times=("09:33:05",),
        )
