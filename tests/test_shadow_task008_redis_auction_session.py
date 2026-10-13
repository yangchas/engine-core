import json
from pathlib import Path

import pytest

from examples.shadow_task008_redis_auction_session import (
    ReadOnlyQ2Client,
    run_shadow,
)
from engine_core import AuctionAnchorSessionV1


class FakeClient:
    def smembers(self, key):
        return {"q2:active:20261009": ["000001", "000002"]}.get(key, [])

    def hgetall(self, key):
        symbol = key.removeprefix("q2:")
        rows = {
            "000001": {"px": "1000", "pc": "990", "amt": "100", "ts": "1791509106000", "a20": "990", "a24": "995", "a25": "1000"},
            "000002": {"px": "2000", "pc": "1990", "amt": "200", "ts": "1791509106000", "a20": "1990", "a24": "1995", "a25": "2000"},
        }
        return rows.get(symbol, {})

    def close(self):
        pass


def test_read_only_client_only_exposes_projection_reads():
    fake = FakeClient()
    readonly = ReadOnlyQ2Client(fake)
    assert readonly.smembers("q2:active:20261009") == ["000001", "000002"]
    assert readonly.hgetall("q2:000001")["a20"] == "990"
    for name in ("set", "hset", "delete", "pipeline"):
        with pytest.raises(RuntimeError):
            getattr(readonly, name)


def test_synthetic_capture_emits_three_anchor_events(tmp_path: Path):
    capture = {
        "contract_version": "RedisQ2ReadCaptureV1",
        "trade_date": "2026-10-09",
        "read_completed_at": "2026-10-09T09:25:06+08:00",
        "reads": [
            {"operation": "smembers", "key": "q2:active:20261009", "value": ["000001", "000002"]},
            {"operation": "hgetall", "key": "q2:000001", "value": {"px": "1000", "pc": "990", "amt": "100", "ts": "1791509106000", "a20": "990", "a24": "995", "a25": "1000"}},
            {"operation": "hgetall", "key": "q2:000002", "value": {"px": "2000", "pc": "1990", "amt": "200", "ts": "1791509106000", "a20": "1990", "a24": "1995", "a25": "2000"}},
        ],
    }
    capture_path = tmp_path / "capture.json"
    capture_path.write_text(json.dumps(capture), encoding="utf-8")
    result_dir = tmp_path / "result"
    run_shadow(source="capture", capture_path=capture_path, out_dir=result_dir)
    events = [json.loads(line) for line in (result_dir / "shadow_events.jsonl").read_text().splitlines()]
    assert [event["tag"] for event in events] == ["0920", "0924", "0925"]
    assert [event["available_count"] for event in events] == [2, 2, 2]


def test_capture_replay_uses_sealed_read_completion_time(tmp_path: Path):
    capture = {
        "contract_version": "RedisQ2ReadCaptureV1",
        "trade_date": "2026-10-09",
        "read_completed_at": "2026-10-09T09:25:12+08:00",
        "reads": [
            {"operation": "smembers", "key": "q2:active:20261009", "value": ["000001"]},
            {"operation": "hgetall", "key": "q2:000001", "value": {
                "px": "1000", "pc": "990", "amt": "100", "ts": "1791509103000",
                "a20": "990", "a24": "995", "a25": "1000",
            }},
        ],
    }
    capture_path = tmp_path / "capture.json"
    capture_path.write_text(json.dumps(capture), encoding="utf-8")
    result = run_shadow(source="capture", capture_path=capture_path, out_dir=tmp_path / "replay")
    events = [json.loads(line) for line in (tmp_path / "replay" / "shadow_events.jsonl").read_text().splitlines()]
    event = next(item for item in events if item["tag"] == "0925")
    from engine_core.clock import local_datetime_ms
    assert event["evaluation_time_ms"] == local_datetime_ms("2026-10-09", "09:25:12")


def test_capture_without_read_completion_uses_source_time(tmp_path: Path):
    capture = {
        "contract_version": "RedisQ2ReadCaptureV1",
        "trade_date": "2026-10-09",
        "reads": [
            {"operation": "smembers", "key": "q2:active:20261009", "value": ["000001"]},
            {"operation": "hgetall", "key": "q2:000001", "value": {
                "px": "1000", "pc": "990", "amt": "100", "ts": "1791509103000",
                "a20": "990", "a24": "995", "a25": "1000",
            }},
        ],
    }
    capture_path = tmp_path / "capture-no-read-time.json"
    capture_path.write_text(json.dumps(capture), encoding="utf-8")
    run_shadow(source="capture", capture_path=capture_path, out_dir=tmp_path / "replay-no-read-time")
    events = [json.loads(line) for line in (tmp_path / "replay-no-read-time" / "shadow_events.jsonl").read_text().splitlines()]
    event = next(item for item in events if item["tag"] == "0925")
    assert event["evaluation_time_ms"] == 1791509103000


def test_first_read_is_recorded_only_after_late_start_is_observed():
    from datetime import datetime, timedelta, timezone

    from engine_core import AuctionAnchorSessionV1, FreshnessPolicy, RedisQ2ProjectionAdapter
    from engine_core.clock import local_datetime_ms
    from examples.shadow_task008_redis_auction_session import _observe_snapshot, _rows

    date = "2026-10-09"
    ms = lambda hms: local_datetime_ms(date, hms)  # noqa: E731
    cst = timezone(timedelta(hours=8))
    class BeforeBarrierClient(FakeClient):
        def hgetall(self, key):
            row = dict(super().hgetall(key))
            if row:
                row["ts"] = "1791509103000"  # 09:25:03, before first-observable barrier
            return row

    client = BeforeBarrierClient()
    session = AuctionAnchorSessionV1(date, ("000001", "000002"))
    first_read: set = set()

    def observe(hms):
        at = datetime.fromtimestamp(ms(hms) / 1000, tz=cst)
        snapshot = RedisQ2ProjectionAdapter(client).read(
            date, freshness_policy=FreshnessPolicy(), observed_at=at)
        events = _observe_snapshot(
            snapshot=snapshot, session=session, rows=_rows(snapshot), first_read=first_read,
            wall_started=at, wall_completed=at)
        return next(e for e in events if e["tag"] == "0925")

    before = observe("09:25:03")
    assert before["late_execution"]["status"] == "BEFORE_FIRST_OBSERVABLE"
    assert "0925" not in first_read
    after = observe("09:25:12")
    assert after["late_execution"]["status"] == "LATE_START_OBSERVED"


def test_live_requires_confirmation_and_redacts_client_errors(tmp_path: Path):
    class FailingClient(FakeClient):
        def hgetall(self, key):
            raise ValueError("secret-host-123")

    with pytest.raises(RuntimeError):
        run_shadow(
            source="live", live_client=object(), out_dir=tmp_path / "unconfirmed",
            trade_date="2026-10-09",
        )

    with pytest.raises(RuntimeError) as exc_info:
        run_shadow(
            source="live", live_client=FailingClient(), out_dir=tmp_path / "redacted",
            trade_date="2026-10-09", confirm_read_only=True,
        )
    assert "secret-host-123" not in str(exc_info.value)


def test_live_redacts_session_errors(monkeypatch, tmp_path: Path):
    def fail_observe(*args, **kwargs):
        raise ValueError("secret-host-123")

    monkeypatch.setattr(AuctionAnchorSessionV1, "observe_late_start", fail_observe)
    with pytest.raises(RuntimeError) as exc_info:
        run_shadow(
            source="live", live_client=FakeClient(), out_dir=tmp_path / "session-error",
            trade_date="2026-10-09", confirm_read_only=True,
        )

    assert "secret-host-123" not in str(exc_info.value)
    assert "secret-host-123" not in str(exc_info.value.__cause__ or "")
    assert exc_info.value.__suppress_context__ is True


def test_live_barrier_opens_when_source_time_stalls_before_first_observable(tmp_path: Path):
    class StalledClient(FakeClient):
        def hgetall(self, key):
            row = dict(super().hgetall(key))
            if row:
                row["ts"] = "1791509103000"  # 09:25:03, before the 0925 first-observable time
            return row

    run_shadow(
        source="live", live_client=StalledClient(), out_dir=tmp_path / "stalled",
        trade_date="2026-10-09", confirm_read_only=True,
    )
    events = [
        json.loads(line)
        for line in (tmp_path / "stalled" / "shadow_events.jsonl").read_text().splitlines()
    ]
    anchor = next(event for event in events if event["tag"] == "0925")
    assert anchor["late_execution"]["status"] == "LATE_START_OBSERVED"
    assert anchor["available_count"] == 2


def test_live_pool_through_live_input_layer_writes_report_and_trace(tmp_path: Path):
    class PoolClient(FakeClient):
        def type(self, key):
            return b"hash" if key == "cache:yest_limit_pool:2026-10-08" else b"none"

        def hlen(self, key):
            return 1

        def hscan_iter(self, key):
            row = {"symbol": "000001", "name": "n", "trade_date": "2026-10-08",
                   "close_pct": 10.0, "lb_days": 1, "turnover": 1.0e8,
                   "plate": "x", "seal_time": "", "source": "kaipan"}
            return iter([(b"000001", json.dumps(row).encode())])

        def get(self, key):
            return None

        def ttl(self, key):
            return -1

    run_shadow(
        source="live", live_client=PoolClient(), out_dir=tmp_path / "livepool",
        trade_date="2026-10-09", confirm_read_only=True,
        live_pool_previous_date="2026-10-08",
    )
    out = tmp_path / "livepool"
    assert "昨日涨停结构" in (out / "report_000.txt").read_text(encoding="utf-8")
    trace = json.loads((out / "live_inputs.jsonl").read_text().splitlines()[0])
    assert trace["status"] == "FRESH" and trace["row_count"] == 1


def test_stalled_source_time_then_late_final_timeline(tmp_path: Path):
    """2026-10-13 live shape: Q2 stamps stall at 09:25:03 until 09:26:00, a
    Shenzhen symbol's a25 is never set, its ph==0 final arrives at 09:26:03."""
    from datetime import datetime, timedelta, timezone

    from engine_core import AuctionAnchorSessionV1, FreshnessPolicy, RedisQ2ProjectionAdapter
    from engine_core.clock import local_datetime_ms
    from examples.shadow_task008_redis_auction_session import _observe_snapshot, _rows

    date = "2026-10-13"
    ms = lambda hms: local_datetime_ms(date, hms)  # noqa: E731
    cst = timezone(timedelta(hours=8))

    def clock(hms):
        return datetime.fromtimestamp(ms(hms) / 1000, tz=cst)

    class TimelineClient:
        def __init__(self, state):
            self.state = state

        def smembers(self, key):
            return ["600000", "000001"] if key == "q2:active:20261013" else []

        def hgetall(self, key):
            return self.state.get(key.removeprefix("q2:"), {})

    base = {"px": "1000", "pc": "990", "amt": "0", "vol": "0", "am": "5000", "br": "0", "ar": "0",
            "a20": "1000", "a24": "1000"}
    state = {
        "600000": dict(base, ts=str(ms("09:25:03")), ph="0", a25="1000", amt="1000000", vol="1000"),
        "000001": dict(base, ts=str(ms("09:25:03")), ph="1", a25="0"),
    }
    session = AuctionAnchorSessionV1(date, ("000001", "600000"), recover_final_from_phase_zero=True)
    first_read: set = set()

    def poll(hms):
        client = TimelineClient(state)
        snap = RedisQ2ProjectionAdapter(client).read(
            date, freshness_policy=FreshnessPolicy(), observed_at=clock(hms))
        events = _observe_snapshot(
            snapshot=snap, session=session, rows=_rows(snap), first_read=first_read,
            wall_started=clock(hms), wall_completed=clock(hms))
        return next(e for e in events if e["tag"] == "0925")

    early = poll("09:25:12")
    assert early["late_execution"]["status"] == "LATE_START_OBSERVED"
    assert (early["available_count"], early["missing_count"]) == (1, 1)
    same = poll("09:25:40")  # source time still 09:25:03: no new content
    assert same["late_execution"]["status"] == "UNCHANGED"
    state["000001"] = dict(base, ts=str(ms("09:26:03")), ph="0", a25="0",
                           px="1012", amt="500000", vol="400")
    late = poll("09:26:05")
    assert late["late_execution"]["status"] == "REVISION_EMITTED"
    assert (late["available_count"], late["missing_count"]) == (2, 0)
    assert set(session.phase_zero_recoveries) == {"000001"}


def test_sealed_live_capture_replays_to_the_same_anchor_events(tmp_path: Path):
    run_shadow(
        source="live", live_client=FakeClient(), out_dir=tmp_path / "live",
        trade_date="2026-10-09", confirm_read_only=True, seal_captures=True,
    )
    sealed = tmp_path / "live" / "capture_000.json"
    capture = json.loads(sealed.read_text(encoding="utf-8"))
    assert capture["contract_version"] == "RedisQ2ReadCaptureV1"
    assert {item["operation"] for item in capture["reads"]} == {"smembers", "hgetall"}
    run_shadow(source="capture", capture_path=sealed, out_dir=tmp_path / "replay")

    def anchors(path):
        return [
            (e["tag"], e["available_count"], e["missing_count"])
            for e in map(json.loads, (path / "shadow_events.jsonl").read_text().splitlines())
        ]

    assert anchors(tmp_path / "live") == anchors(tmp_path / "replay")


def test_capture_dir_replays_three_live_polls_as_one_session(tmp_path: Path, monkeypatch):
    from datetime import datetime
    import examples.shadow_task008_redis_auction_session as module
    from engine_core.clock import local_datetime_ms

    date = "2026-10-09"
    times = ["09:25:12", "09:25:40", "09:26:05"]
    current = 0
    base = {"px": "1000", "pc": "990", "amt": "0", "vol": "0", "am": "5000",
            "br": "0", "ar": "0", "a20": "1000", "a24": "1000"}
    states = [
        {"000001": dict(base, ts=str(local_datetime_ms(date, "09:25:03")), ph="1", a25="0"),
         "000002": dict(base, ts=str(local_datetime_ms(date, "09:25:03")), ph="1", a25="0")},
        {"000001": dict(base, ts=str(local_datetime_ms(date, "09:25:03")), ph="1", a25="1000"),
         "000002": dict(base, ts=str(local_datetime_ms(date, "09:25:03")), ph="1", a25="0")},
        {"000001": dict(base, ts=str(local_datetime_ms(date, "09:25:03")), ph="1", a25="1000"),
         "000002": dict(base, ts=str(local_datetime_ms(date, "09:26:03")), ph="0", a25="0",
                         px="1012", amt="500000", vol="400")},
    ]

    class TimelineClient:
        def smembers(self, key):
            nonlocal current
            if key == "q2:active:20261009":
                result = ["000001", "000002"]
                current += 1
                return result
            return []

        def hgetall(self, key):
            return states[min(current - 1, 2)].get(key.removeprefix("q2:"), {})

        def close(self):
            pass

    def clock():
        value = times[min(max(current - 1, 0), 2)]
        from zoneinfo import ZoneInfo
        return datetime.fromisoformat(f"{date}T{value}").replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(module, "_aware_now", clock)
    adapter = module.RedisQ2ProjectionAdapter
    class TimedAdapter(adapter):
        def read(self, trade_date, observed_at=None, **kwargs):
            return super().read(trade_date, observed_at=observed_at or clock(), **kwargs)
    monkeypatch.setattr(module, "RedisQ2ProjectionAdapter", TimedAdapter)
    live_dir = tmp_path / "live-many"
    run_shadow(source="live", live_client=TimelineClient(), out_dir=live_dir,
               trade_date=date, confirm_read_only=True, max_polls=3,
               recover_final_from_phase_zero=True, seal_captures=True)

    replay_dir = tmp_path / "replay-many"
    run_shadow(source="capture", capture_dir=live_dir, out_dir=replay_dir,
               recover_final_from_phase_zero=True)

    def events(path):
        rows = [json.loads(line) for line in (path / "shadow_events.jsonl").read_text().splitlines()]
        for row in rows:
            row.pop("wall_read_started", None)
            row.pop("wall_read_completed", None)
        return rows

    assert events(live_dir) == events(replay_dir)
    final = [row for row in events(replay_dir) if row["tag"] == "0925"][-1]
    assert final["available_count"] == 2 and final["phase_zero_recovered_count"] == 1
    manifest = json.loads((replay_dir / "manifest.json").read_text())
    assert [item["file"] for item in manifest["captures"]] == [
        "capture_000.json", "capture_001.json", "capture_002.json"]
    assert all(len(item["sha256"]) == 64 for item in manifest["captures"])


def test_capture_dir_rejects_empty_directory_and_conflicting_cli_args(tmp_path: Path, monkeypatch):
    from examples.shadow_task008_redis_auction_session import main

    with pytest.raises(ValueError, match="no capture_.*json"):
        run_shadow(source="capture", capture_dir=tmp_path / "empty", out_dir=tmp_path / "empty-out")
    import sys
    monkeypatch.setattr(sys, "argv", ["shadow", "--source", "capture", "--capture", "a.json",
                                      "--capture-dir", "captures", "--out-dir", "out"])
    with pytest.raises(SystemExit):
        main()


def test_capture_dir_sorts_numeric_sequences_ignores_other_names_and_lists_them(tmp_path: Path):
    capture_dir = tmp_path / "captures"
    capture_dir.mkdir()
    capture = {
        "contract_version": "RedisQ2ReadCaptureV1",
        "trade_date": "2026-10-09",
        "read_completed_at": "2026-10-09T09:25:12+08:00",
        "reads": [
            {"operation": "smembers", "key": "q2:active:20261009", "value": ["000001"]},
            {"operation": "hgetall", "key": "q2:000001", "value": {
                "px": "1000", "pc": "990", "amt": "100", "ts": "1791509103000",
                "a20": "990", "a24": "995", "a25": "1000",
            }},
        ],
    }
    for sequence in (2, 10, 101, 1000):
        (capture_dir / f"capture_{sequence}.json").write_text(json.dumps(capture), encoding="utf-8")
    (capture_dir / "capture_notes.json").write_text("{}", encoding="utf-8")
    (capture_dir / "other.json").write_text("{}", encoding="utf-8")

    out_dir = tmp_path / "replay"
    run_shadow(source="capture", capture_dir=capture_dir, out_dir=out_dir)

    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert [item["file"] for item in manifest["captures"]] == [
        "capture_2.json", "capture_10.json", "capture_101.json", "capture_1000.json"]
    assert manifest["ignored_files"] == ["capture_notes.json", "other.json"]


def test_capture_dir_rejects_duplicate_numeric_sequences(tmp_path: Path):
    capture_dir = tmp_path / "captures"
    capture_dir.mkdir()
    for name in ("capture_1.json", "capture_001.json"):
        (capture_dir / name).write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate capture sequence.*1"):
        run_shadow(source="capture", capture_dir=capture_dir, out_dir=tmp_path / "replay")


def test_live_session_records_cold_start_assessment(tmp_path: Path, monkeypatch):
    import examples.shadow_task008_redis_auction_session as module

    # the fake client's rows are from 2026-10-09; "now" is another date
    run_shadow(
        source="live", live_client=FakeClient(), out_dir=tmp_path / "cs",
        trade_date="2026-10-09", confirm_read_only=True,
    )
    record = json.loads((tmp_path / "cs" / "cold_start.json").read_text(encoding="utf-8"))
    assert record["status"] == "START_DATE_MISMATCH" and record["trade_date"] == "2026-10-09"

    # same-date start: assessed
    from engine_core.clock import local_datetime_ms

    out = tmp_path / "cs2"
    out.mkdir()
    module._write_cold_start(out, "2026-10-09", local_datetime_ms("2026-10-09", "09:27:00"))
    record = json.loads((out / "cold_start.json").read_text(encoding="utf-8"))
    assert record["start_class"] == "AUCTION_FINAL"
    assert record["late_final_recovery"] == "WINDOW_OPEN"


def test_td_reconstruction_written_when_cold_start_needs_it(tmp_path: Path):
    import examples.shadow_task008_redis_auction_session as module
    from engine_core.clock import local_datetime_ms

    rows = [
        {"symbol": "000001", "auction_tag": tag, "px_milli": 10000, "chg_bp": 10,
         "match_amt_yuan": amt, "rest_bid_amt_yuan": 0, "rest_ask_amt_yuan": 0,
         "limit_state": 0, "trade_date": "20261009", "ts": "2026-10-09 09:25:06"}
        for tag, amt in (("0920", 1), ("0924", 2), ("0925", 3))
    ]
    td = tmp_path / "td.jsonl"
    td.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    out = tmp_path / "o"
    out.mkdir()
    record = module._write_cold_start(out, "2026-10-09", local_datetime_ms("2026-10-09", "10:00:00"))
    assert record["theme_amount_delta_0924_0925"] == "TD_RECONSTRUCTION_REQUIRED"
    module._write_td_reconstruction(out, "2026-10-09", td)
    trace = json.loads((out / "td_reconstruction.json").read_text(encoding="utf-8"))
    assert trace["status"] == "READY" and trace["summaries"]["0925"]["auction_amount_yuan"] == 3
    assert len(trace["td_rows_sha256"]) == 64
