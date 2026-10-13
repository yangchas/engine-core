from __future__ import annotations

import json
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize(
    "module_name",
    [
        "examples.shadow_task008_redis_auction_session",
        "examples.run_task008_q2frame_auction_engine_shadow",
        "examples.run_task008_auction_report_replay",
    ],
)
def test_recovery_cli_defaults_on_and_supports_explicit_disable(module_name):
    import importlib

    parser = importlib.import_module(module_name)._build_parser()
    assert parser.parse_args(_required_cli_args(module_name)).recover_final_from_phase_zero is True
    assert parser.parse_args(
        _required_cli_args(module_name) + ["--no-recover-final-from-phase-zero"]
    ).recover_final_from_phase_zero is False


def _required_cli_args(module_name):
    if module_name.endswith("redis_auction_session"):
        return ["--source", "capture", "--capture", "capture.json", "--out-dir", "out"]
    if module_name.endswith("q2frame_auction_engine_shadow"):
        return ["--q2frame", "frames.jsonl", "--trade-date", "2026-10-09", "--output", "out.json"]
    return [
        "--trade-date", "2026-10-09", "--q2frame", "frames.jsonl", "--cutoff", "09:27:10",
        "--td-rows", "td.jsonl", "--plate-snapshot", "plates.json", "--pool", "pool.json",
        "--out-dir", "out",
    ]


def test_recovered_anchor_report_retains_ph0_source_label(tmp_path, monkeypatch):
    from engine_core import AuctionAnchorSessionV1
    from engine_core.clock import local_datetime_ms
    import examples.shadow_task008_redis_auction_session as shadow

    trade_date = "2026-10-09"
    source_time = local_datetime_ms(trade_date, "09:26:33")
    session = AuctionAnchorSessionV1(trade_date, ("000001",))
    session.observe(
        "0925",
        {"000001": {
            "phase": 0, "price_milli": 12_950, "volume_lots": 120,
            "amount_yuan": 155_400, "source_record_time_ms": source_time,
        }},
        evaluation_time_ms=source_time,
    )
    quote = SimpleNamespace(
        auction_anchor_0925_price_milli=None, pre_close_milli=12_000,
        auction_amount_yuan=155_400, price_milli=12_950,
        source_record_time_ms=source_time,
    )
    snapshot = SimpleNamespace(quotes={"000001": quote})
    monkeypatch.setattr(
        shadow,
        "build_shadow_auction_report",
        lambda **_: SimpleNamespace(text="", as_mapping=lambda: {}),
    )

    shadow._write_report(
        tmp_path, 0, snapshot, session, None, trade_date,
        SimpleNamespace(timestamp=lambda: source_time / 1000), "fixture",
    )

    table = json.loads((tmp_path / "anchor_table_000.json").read_text())
    assert table["000001"]["src"] == "ph0_recovered"
