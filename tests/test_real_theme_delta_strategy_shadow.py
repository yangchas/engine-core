from __future__ import annotations

from examples.run_real_theme_delta_strategy_shadow import run_real_theme_delta_strategy_shadow


class _FakeRedis:
    def hgetall(self, key):
        return {}


def test_real_theme_strategy_shadow_fails_closed_without_projections(monkeypatch):
    monkeypatch.setattr(
        "run_real_theme_delta_strategy_shadow.read_redis_auction_projection",
        lambda *args, **kwargs: (),
    )
    result = run_real_theme_delta_strategy_shadow(client=_FakeRedis(), trade_date="2026-09-18")
    assert result["status"] == "UNAVAILABLE"
    assert result["read_only"] is True
