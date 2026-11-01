import json


def _backfill_cli(monkeypatch, tmp_path, fake_fill):
    from examples import backfill_td_daily_kline
    from engine_core.data_pipeline import daily_kline_fill
    from engine_core.guardian import daily_kline_coverage

    class Store:
        def close(self):
            pass

    monkeypatch.setenv("TD_CONFIG", "configured")
    monkeypatch.setattr(daily_kline_coverage, "td_settings", lambda **_kwargs: {})
    monkeypatch.setattr(daily_kline_coverage, "make_store", lambda _settings: Store())
    monkeypatch.setattr(daily_kline_fill, "run_daily_kline_fill_from_root", fake_fill)
    return backfill_td_daily_kline.main(["--date", "2026-10-20", "--artifact-root", str(tmp_path),
                                         "--baostock-no-progress-seconds", "47"])


def test_backfill_cli_runs_the_td_fill_and_forwards_guardian_no_progress_budget(monkeypatch, capsys, tmp_path):
    seen = {}

    def fake_fill(root, target, *, args=None, store=None, **_kwargs):
        seen.update(root=str(root), target=target, args=args)
        return {"status": "COMPLETE", "attempted": 3,
                "partitions": [{"trade_date": target, "row_count": 1, "version": "v1"}]}

    assert _backfill_cli(monkeypatch, tmp_path, fake_fill) == 0
    assert seen["args"]["baostock_no_progress_seconds"] == 47.0
    assert seen["target"] == "2026-10-20"
    payload = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert (payload["status"], payload["version"], payload["attempts"]) == ("ok", "v1", 3)


def test_backfill_cli_skips_when_another_trigger_holds_the_lease(monkeypatch, capsys, tmp_path):
    code = _backfill_cli(monkeypatch, tmp_path, lambda *_a, **_k: {"status": "BUSY"})
    assert code == 20 and json.loads(capsys.readouterr().out.splitlines()[-1])["status"] == "not_applicable"


def test_backfill_cli_fail_fast_is_a_retryable_failure(monkeypatch, capsys, tmp_path):
    def fake_fill(*_a, **_k):
        raise RuntimeError("BAOSTOCK_GAP_UNRESOLVED:consecutive_failures:attempted=5")

    assert _backfill_cli(monkeypatch, tmp_path, fake_fill) == 30
    payload = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert (payload["status"], payload["retryable"]) == ("failed", True)


def test_daily_kline_backfill_not_eligible_is_not_applicable(monkeypatch):
    from examples import guardian_handler
    from engine_core.guardian import daily_kline_backfill

    monkeypatch.setattr(daily_kline_backfill, "run_daily_kline_backfill",
                        lambda *_args, **_kwargs: {"status": "NOT_ELIGIBLE", "range_symbols": 0})
    request = {"contract": 1, "event": "daily_kline_backfill", "instance": "daily_kline_backfill@08:00",
               "trade_date": "2026-10-29", "data_date": "2026-10-28", "args": {}}
    code, payload = guardian_handler._run(request)
    assert (code, payload["status"], payload["error_code"], payload["retryable"]) == (
        0, "ok", "", False)


def test_rehearsal_can_queue_one_real_fetch_item_instance(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from examples import rehearse_event

    schedule = rehearse_event.load_schedule()
    prepared, event_ids = rehearse_event.prepare_schedule(
        schedule, "fetch_item:daily_kline", [], "rh-c599-check",
        datetime(2026, 10, 29, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai")), 3,
    )
    event = prepared["events"][0]
    assert event["event_id"] == "fetch_item"
    assert event["after"] == []
    assert event["args"]["baostock_no_progress_seconds"] == 300
    assert event_ids["fetch_item:daily_kline"] == "fetch_item:daily_kline"

    rehearse_event.seed_fetch_item_state(
        tmp_path / "state", event_ids["fetch_item:daily_kline"], "2026-10-20",
        "2026-10-29T08:03:00+08:00",
    )
    snapshot = json.loads((tmp_path / "state/ledger/snapshot-2026-10-29.json").read_text())
    state = snapshot["events"]["fetch_item:daily_kline"]
    assert state["data_date"] == "2026-10-20"
    assert state["next_attempt_at"] == "2026-10-29T08:03:00+08:00"
