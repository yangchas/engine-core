import json

import pytest

from engine_core.daily_pipeline import DailySources, run_daily_pipeline


class FakeTD:
    def states_at(self, cutoff):
        return {}

    def checkpoint_ready(self, cutoff):
        return False

    def latest_tick_ts(self):
        return None


class FakeRedis:
    def __init__(self):
        self.writes = []

    def scan_iter(self, **kwargs):
        return iter(())

    def set(self, key, value, **kwargs):
        self.writes.append((key, value, kwargs))


def test_three_stages_write_reports_and_explicit_missing_references(tmp_path):
    references = tmp_path / "references"
    output = tmp_path / "reports"
    sources = DailySources(td=FakeTD(), reference_root=references)
    for stage in ("auction", "open", "close"):
        result = run_daily_pipeline(stage=stage, trade_date="2026-10-15", out_root=output,
                                    reference_root=references, sources=sources, dry_run=True)
        folder = result["out_dir"]
        assert (folder / "report.html").is_file()
        assert (folder / "report.json").is_file()
        manifest = json.loads((folder / "manifest.json").read_text())
        assert manifest["degraded"]
        assert any(item.startswith("MISSING_REFERENCE:") for item in manifest["degraded"])
        assert manifest["report_json_bytes"] <= 300_000
    assert (output / "2026-10-15" / "auction_0926" / "report.json").exists()
    assert (output / "2026-10-15" / "open_0946" / "report.json").exists()
    assert (output / "2026-10-15" / "close_review" / "report.json").exists()


def test_repeat_run_uses_numbered_directory_without_overwriting(tmp_path):
    kwargs = dict(stage="auction", trade_date="2026-10-15", out_root=tmp_path / "out",
                  reference_root=tmp_path / "refs", sources=DailySources(td=FakeTD()))
    first = run_daily_pipeline(**kwargs)
    marker = first["out_dir"] / "marker"
    marker.write_text("keep")
    second = run_daily_pipeline(**kwargs)
    assert second["out_dir"] == tmp_path / "out" / "2026-10-15" / "auction_0926-001"
    assert marker.read_text() == "keep"


def test_publish_only_uses_report_prefix_and_two_day_ttl(tmp_path):
    redis = FakeRedis()
    run_daily_pipeline(stage="auction", trade_date="2026-10-15", out_root=tmp_path / "out",
                       reference_root=tmp_path / "refs",
                       sources=DailySources(td=FakeTD(), redis=redis), publish=True)
    assert len(redis.writes) == 1
    key, value, options = redis.writes[0]
    assert key == "core:cache:report:2026-10-15:auction"
    assert key.startswith("core:cache:")
    assert options == {"ex": 172800}
    assert json.loads(value)["stage"] == "auction_0926"


def test_publish_and_dry_run_are_mutually_exclusive(tmp_path):
    with pytest.raises(ValueError, match="cannot be combined"):
        run_daily_pipeline(stage="auction", trade_date="2026-10-15", out_root=tmp_path,
                           sources=DailySources(td=FakeTD()), publish=True, dry_run=True)


def test_close_stage_captures_injected_td_daily_kline(tmp_path):
    sources = DailySources(td=FakeTD(), reference_root=tmp_path / "refs",
                           daily_kline_rows=lambda day: [{
                               "ts": f"{day} 00:00:00", "open": 10, "high": 11, "low": 9,
                               "close": 10.5, "volume": 100, "amount": 1000,
                               "turnover": 1.2, "pct_chg": 5, "tbname": "d_000001"}])
    result = run_daily_pipeline(stage="close", trade_date="2026-10-15", out_root=tmp_path / "out",
                                reference_root=tmp_path / "refs", sources=sources, dry_run=True)
    capture = result["out_dir"] / "daily_capture"
    meta = json.loads((capture / "manifest.json").read_text())
    row = json.loads((capture / "daily_kline.jsonl").read_text())
    assert meta["source"] == "td_daily_kline"
    assert row["date"] == "2026-10-15"
    assert row["symbol"] == "000001"
