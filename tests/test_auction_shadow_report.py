from dataclasses import replace

from engine_core.clock import local_datetime_ms
from engine_core.auction_shadow_report import build_shadow_auction_report
from engine_core.q2 import normalize_q2


def _quote(symbol, **raw):
    base = {"px": "10000", "pc": "10000", "ph": "0", "vol": "100", "amt": "1000000", "ts": "1760000000000"}
    base.update(raw)
    return normalize_q2(symbol, base)


def test_report_without_pool_notes_it_and_applies_recovery():
    quote = _quote("000001")
    report = build_shadow_auction_report(
        trade_date="2026-10-09",
        quotes={"000001": replace(quote, auction_anchor_0925_price_milli=None)},
        observation_time_ms=1_760_000_000_000,
        recoveries={"000001": {"price_milli": 10_000, "source_time_ms": 1}},
    )
    assert "previous-day pool not supplied" in report.text
    assert "recovered from late ph==0 snapshots" in report.text
    assert report.component_status["market_summary"] in ("READY", "PARTIAL")


def test_report_notes_collecting_phase_before_0925():
    early = build_shadow_auction_report(
        trade_date="2026-10-12", quotes={}, observation_time_ms=local_datetime_ms("2026-10-12", "09:17:40"),
    )
    late = build_shadow_auction_report(
        trade_date="2026-10-12", quotes={}, observation_time_ms=local_datetime_ms("2026-10-12", "09:25:30"),
    )
    assert "observed before 09:25:00" in early.text
    assert "observed before 09:25:00" not in late.text
