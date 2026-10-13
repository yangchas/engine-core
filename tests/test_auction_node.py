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


class FreshAuctionClient(StrictAuctionClient):
    def __init__(self, poll_times):
        super().__init__()
        self.poll_times = iter(poll_times)
        self.current_time = "09:25:00"

    def smembers(self, key):
        self.calls.append("smembers")
        self.current_time = next(self.poll_times)
        return ["000001", "000002"]

    def hgetall(self, key):
        row = super().hgetall(key)
        row["ts"] = str(local_datetime_ms("2026-10-13", self.current_time))
        return row


def _node(tmp_path: Path, client, ledger, times, sleep_until):
    return run_auction_node(
        trade_date="2026-10-13", out_dir=tmp_path / "node", live_client=client,
        ledger_path=ledger, read_times=times, confirm_read_only=True,
        sleep_until=sleep_until,
    )


def _use_q2_observation_times(monkeypatch, times):
    from datetime import datetime, timezone
    import examples.shadow_task008_redis_auction_session as shadow
    from engine_core.q2 import RedisQ2ProjectionAdapter
    observations = iter(times)

    class TimedAdapter(RedisQ2ProjectionAdapter):
        def __init__(self, client):
            super().__init__(client)

        def read(self, trade_date, observed_at=None, **kwargs):
            hms = next(observations)
            observed_at = datetime.fromtimestamp(
                local_datetime_ms(trade_date, hms) / 1000, tz=timezone.utc,
            )
            return super().read(trade_date, observed_at=observed_at, **kwargs)

    monkeypatch.setattr(shadow, "RedisQ2ProjectionAdapter", TimedAdapter)


def test_auction_node_dispatches_timers_and_restores_them_on_restart(tmp_path, monkeypatch):
    client = FreshAuctionClient(("09:25:00", "09:26:00", "09:32:00"))
    ledger = tmp_path / "ledger.jsonl"
    times = ("09:25:12", "09:26:05", "09:32:05")
    clock = lambda date, hms: local_datetime_ms(date, hms)  # noqa: E731
    _use_q2_observation_times(monkeypatch, ("09:25:12", "09:26:05", "09:32:05") * 2)
    first = _node(tmp_path, client, ledger, times, clock)
    assert first["timers_acknowledged"] == ["AUCTION_0926", "OPENING_0932"]
    for timer_id in first["timers_acknowledged"]:
        row = json.loads((tmp_path / "node" / f"timer_{timer_id}.json").read_text())
        assert row["origin"] == "NORMAL"
        assert row["cutoff_fidelity"] == "AT_CUTOFF"
        if timer_id == "AUCTION_0926":
            assert row["q2_readiness"] == "READY"
        else:
            assert row["q2_readiness"] == "READY"
        assert row["anchor_table_sha256"]
        assert any((tmp_path / "node").glob("shadow_run_*" + "/" + row["anchor_table_file"]))
    ledger_lines = ledger.read_text().splitlines()
    second = _node(
        tmp_path, FreshAuctionClient(("09:25:00", "09:26:00", "09:32:00")),
        ledger, times, clock,
    )
    assert second["timers_acknowledged"] == []
    assert second["timers_restored_from_ledger"] == ["AUCTION_0926", "OPENING_0932"]
    assert ledger.read_text().splitlines() == ledger_lines
    assert set(client.calls) <= {"smembers", "hgetall", "type", "hlen", "hscan_iter", "get", "ttl"}


def test_late_first_poll_marks_both_timers_as_recovery_catchup(tmp_path, monkeypatch):
    client = FreshAuctionClient(("09:32:00",))
    late_times = ("09:33:05",)
    clock = lambda date, hms: local_datetime_ms(date, hms)  # noqa: E731
    _use_q2_observation_times(monkeypatch, ("09:33:05",))
    result = _node(tmp_path, client, tmp_path / "ledger.jsonl", late_times, clock)
    assert result["timers_acknowledged"] == ["AUCTION_0926", "OPENING_0932"]
    assert all(
        json.loads((tmp_path / "node" / f"timer_{timer_id}.json").read_text())["origin"]
        == "RECOVERY_CATCHUP"
        for timer_id in result["timers_acknowledged"]
    )
    assert all(
        json.loads((tmp_path / "node" / f"timer_{timer_id}.json").read_text())[
            "cutoff_fidelity"
        ] == "AFTER_CUTOFF_CURRENT_STATE"
        for timer_id in result["timers_acknowledged"]
    )


def test_opening_timer_waits_for_fresh_q2_then_dispatches(tmp_path, monkeypatch):
    class StaleThenFreshClient(FreshAuctionClient):
        def __init__(self):
            super().__init__(("09:00:00", "09:33:06"))

        def hgetall(self, key):
            row = super().hgetall(key)
            if self.current_time == "09:00:00":
                row["ts"] = str(local_datetime_ms("2026-10-12", "09:00:00"))
            return row

    ledger = tmp_path / "ledger.jsonl"
    _use_q2_observation_times(monkeypatch, ("09:33:05", "09:33:06"))
    result = _node(
        tmp_path, StaleThenFreshClient(), ledger, ("09:33:05", "09:33:06"),
        lambda day, hms: local_datetime_ms(day, hms),
    )

    node_run = json.loads((tmp_path / "node" / "node_run.json").read_text())
    assert result["timers_acknowledged"] == ["AUCTION_0926", "OPENING_0932"]
    assert node_run["timers_acknowledged"] == ["AUCTION_0926", "OPENING_0932"]
    opening = json.loads((tmp_path / "node" / "timer_OPENING_0932.json").read_text())
    auction = json.loads((tmp_path / "node" / "timer_AUCTION_0926.json").read_text())
    assert auction["q2_readiness"] == "PARTIAL"
    assert opening["poll"] == 1
    assert opening["cutoff_fidelity"] == "AFTER_CUTOFF_CURRENT_STATE"


def test_opening_cutoff_uses_actual_execution_poll_after_readiness_delay(tmp_path, monkeypatch):
    class PartialThenReadyClient(FreshAuctionClient):
        def __init__(self):
            super().__init__(("09:25:00", "09:32:00", "09:40:00"))

        def hgetall(self, key):
            row = super().hgetall(key)
            if self.current_time == "09:32:00":
                row["ts"] = str(local_datetime_ms("2026-10-12", "09:00:00"))
            return row

    _use_q2_observation_times(monkeypatch, ("09:25:00", "09:32:05", "09:40:00"))
    result = _node(
        tmp_path, PartialThenReadyClient(), tmp_path / "ledger.jsonl",
        ("09:25:00", "09:32:05", "09:40:00"),
        lambda day, hms: local_datetime_ms(day, hms),
    )
    opening = json.loads((tmp_path / "node" / "timer_OPENING_0932.json").read_text())
    assert result["timers_acknowledged"] == ["AUCTION_0926", "OPENING_0932"]
    assert opening["cutoff_fidelity"] == "AFTER_CUTOFF_CURRENT_STATE"
    assert opening["executed_at_ms"] == local_datetime_ms("2026-10-13", "09:40:00")
    assert opening["executed_poll"] == 2
    assert opening["q2_readiness"] == "READY"


def test_opening_ready_on_first_cutoff_poll_is_at_cutoff(tmp_path, monkeypatch):
    _use_q2_observation_times(monkeypatch, ("09:25:00", "09:26:05", "09:32:05"))
    _node(
        tmp_path, FreshAuctionClient(("09:25:00", "09:26:00", "09:32:00")),
        tmp_path / "ledger.jsonl", ("09:25:00", "09:26:05", "09:32:05"),
        lambda day, hms: local_datetime_ms(day, hms),
    )
    opening = json.loads((tmp_path / "node" / "timer_OPENING_0932.json").read_text())
    assert opening["cutoff_fidelity"] == "AT_CUTOFF"
    assert opening["executed_at_ms"] == local_datetime_ms("2026-10-13", "09:32:05")
    assert opening["executed_poll"] == 2
    assert opening["q2_readiness"] == "READY"


def test_opening_timer_waits_for_q2_when_snapshot_is_empty(tmp_path, monkeypatch):
    class EmptyQ2Client(StrictAuctionClient):
        def smembers(self, key):
            self.calls.append("smembers")
            return []

    ledger = tmp_path / "ledger.jsonl"
    _use_q2_observation_times(monkeypatch, ("09:33:05",))
    _node(
        tmp_path, EmptyQ2Client(), ledger, ("09:33:05",),
        lambda day, hms: local_datetime_ms(day, hms),
    )
    node_run = json.loads((tmp_path / "node" / "node_run.json").read_text())
    assert node_run["timers_acknowledged"] == ["AUCTION_0926"]
    assert node_run["status"] == "DONE_WITH_PENDING"
    assert node_run["timers_pending"] == ["OPENING_0932"]
    assert "OPENING_0932" not in ledger.read_text()
    assert not (tmp_path / "node" / "timer_OPENING_0932.json").exists()


def test_timer_artifact_is_rewritten_identically_after_ledger_failure(tmp_path, monkeypatch):
    from engine_core.execution_ledger import ExecutionLedger

    original_record = ExecutionLedger.record
    failed = False

    def fail_once(self, trade_date, firing):
        nonlocal failed
        if firing.timer_id == "OPENING_0932" and not failed:
            failed = True
            raise OSError("injected ledger failure")
        return original_record(self, trade_date, firing)

    monkeypatch.setattr(ExecutionLedger, "record", fail_once)
    ledger = tmp_path / "ledger.jsonl"
    times = ("09:25:12", "09:26:05", "09:32:05")
    clock = lambda date, hms: local_datetime_ms(date, hms)  # noqa: E731
    _use_q2_observation_times(monkeypatch, ("09:25:12", "09:26:05", "09:32:05") * 2)
    with pytest.raises(RuntimeError, match="live shadow failed"):
        _node(
            tmp_path, FreshAuctionClient(("09:25:12", "09:26:05", "09:32:05")),
            ledger, times, clock,
        )
    timer_path = tmp_path / "node" / "timer_OPENING_0932.json"
    first_contents = timer_path.read_bytes()

    result = _node(
        tmp_path, FreshAuctionClient(("09:25:12", "09:26:05", "09:32:05")),
        ledger, times, clock,
    )
    assert result["timers_acknowledged"] == ["OPENING_0932"]
    assert timer_path.read_bytes() == first_contents


def test_auction_node_requires_read_only_confirmation(tmp_path):
    with pytest.raises(RuntimeError, match="confirm_read_only"):
        run_auction_node(
            trade_date="2026-10-13", out_dir=tmp_path / "node",
            live_client=StrictAuctionClient(), ledger_path=tmp_path / "ledger.jsonl",
            read_times=("09:33:05",),
        )
