from engine_core.slices import SliceReader


def test_auction_slice_falls_back_when_td_coverage_is_low_and_keeps_missing_amount():
    from engine_core.slices.market_fallbacks import MarketFallbackProviders

    calls = []

    def td(request):
        calls.append("td")
        return {"rows": [{"symbol": "600001", "price_milli": 10100,
                          "available_at": "2026-10-16T09:20:03+08:00"}],
                "expected_count": 2, "available_at": "2026-10-16T09:20:03+08:00"}

    def redis(request):
        calls.append("redis")
        return {"rows": [{"symbol": "600001", "price_milli": 10100,
                          "match_amount_yuan": None,
                          "available_at": "2026-10-16T09:20:04+08:00"},
                         {"symbol": "600002", "price_milli": 9900,
                          "match_amount_yuan": None,
                          "available_at": "2026-10-16T09:20:04+08:00"}],
                "expected_count": 2, "available_at": "2026-10-16T09:20:04+08:00"}

    providers = MarketFallbackProviders(
        auction_td=td, auction_redis=redis,
        auction_wencai=lambda request: {"rows": [], "expected_count": 2},
        coverage_threshold=0.9,
    )
    result = SliceReader(providers.providers()).get(
        "auction_snapshot", trade_date="2026-10-16", asof="2026-10-16T09:21:00+08:00",
        tag="0920",
    )

    assert calls == ["td", "redis"]
    assert result.meta["source"] == "legacy_redis_q2"
    assert result.meta["status"] == "DEGRADED"
    assert result.meta["degraded_reason"] == "TD_COVERAGE_BELOW_THRESHOLD"
    assert result.rows[0]["match_amount_yuan"] is None
    assert result.meta["lineage"][0]["source"] == "td_auction_snapshot_v2"
    assert result.meta["lineage"][1]["source"] == "legacy_redis_q2"


def test_auction_slice_prefers_td_after_late_source_arrives_and_marks_wencai_semantics():
    from engine_core.slices.market_fallbacks import MarketFallbackProviders

    current = {"rows": [], "expected_count": 1, "available_at": "2026-10-16T09:20:00+08:00"}
    providers = MarketFallbackProviders(
        auction_td=lambda request: current,
        auction_redis=lambda request: {"rows": [], "expected_count": 1,
                                       "available_at": "2026-10-16T09:20:01+08:00"},
        auction_wencai=lambda request: {"rows": [{"symbol": "600001", "match_price": 10,
                                                   "match_amount_yuan": 5000000}],
                                        "expected_count": 1,
                                        "available_at": "2026-10-16T09:20:02+08:00"},
        coverage_threshold=1.0,
    )
    reader = SliceReader(providers.providers())
    first = reader.get("auction_snapshot", trade_date="2026-10-16",
                       asof="2026-10-16T09:20:30+08:00", tag="0925")
    assert first.meta["source"] == "wencai"
    assert first.meta["status"] == "DEGRADED"
    assert first.meta["quality"]["semantics"] == "WENCAI_AUCTION_NOT_TD_FINAL"

    current.update({"rows": [{"symbol": "600001", "price_milli": 10100,
                               "match_amount_yuan": 123456,
                               "event_time": "2026-10-16T09:20:40+08:00",
                               "available_at": "2026-10-16T09:20:40+08:00"}],
                    "available_at": "2026-10-16T09:20:40+08:00"})
    second = reader.get("auction_snapshot", trade_date="2026-10-16",
                        asof="2026-10-16T09:21:00+08:00", tag="0925")
    assert second.meta["source"] == "td_auction_snapshot_v2"
    assert second.meta["status"] == "READY"
    assert second.rows[0]["match_amount_yuan"] == 123456


def test_market_slice_filters_late_rows_and_never_turns_missing_values_into_zero():
    from engine_core.slices.market_fallbacks import MarketFallbackProviders

    providers = MarketFallbackProviders(
        auction_td=lambda request: {"rows": [
            {"symbol": "600001", "price_milli": 10000,
             "available_at": "2026-10-16T09:20:00+08:00"},
            {"symbol": "600002", "price_milli": 10000,
             "available_at": "2026-10-16T09:21:00+08:00"}],
            "expected_count": 2, "available_at": "2026-10-16T09:20:00+08:00"},
        auction_redis=lambda request: None,
        auction_wencai=lambda request: None,
        coverage_threshold=1.0,
    )
    result = SliceReader(providers.providers()).get(
        "auction_snapshot", trade_date="2026-10-16", asof="2026-10-16T09:20:30+08:00",
        tag="0920",
    )
    assert [row["symbol"] for row in result.rows] == ["600001"]
    assert result.rows[0].get("match_amount_yuan") is None
    assert result.meta["status"] == "PARTIAL"
    assert result.meta["reason"] == "NO_ASOF"


def test_limit_state_derivation_and_wencai_pool_are_evidence_not_seal_truth():
    from engine_core.slices.market_fallbacks import MarketFallbackProviders

    providers = MarketFallbackProviders(
        limit_inputs=lambda request: {"rows": [{"symbol": "600001", "pre_close": 10,
                                                  "price": 11, "available_at": "2026-10-16T09:30:00+08:00"}],
                                      "expected_count": 1, "available_at": "2026-10-16T09:30:00+08:00"},
        limit_q2=lambda request: None,
        limit_wencai=lambda request: {"rows": [{"symbol": "600001", "sealed": True}],
                                      "expected_count": 1,
                                      "available_at": "2026-10-16T09:31:00+08:00"},
        coverage_threshold=1.0,
    )
    result = SliceReader(providers.providers()).get(
        "limit_up_today", trade_date="2026-10-16", asof="2026-10-16T09:32:00+08:00",
    )
    assert result.rows[0]["at_limit_up_price"] is True
    assert result.rows[0]["sealed"] is None
    assert result.meta["source"] == "price_derived"


def test_wencai_market_gate_opens_on_first_403_and_blocks_followup_calls(tmp_path):
    from engine_core.data_pipeline.providers.market_fallback_chain import WencaiMarketGate

    calls = []

    def fetch(query, loop, cap):
        calls.append(query)
        return {"status_code": 403}

    gate = WencaiMarketGate(fetch, state_path=tmp_path / "wencai.json",
                            clock_fn=lambda: 1000.0, sleep_fn=lambda _: None)
    assert gate.fetch("query", True, 1000)["reason"] == "provider_blocked"
    assert gate.fetch("query2", True, 1000)["reason"] == "circuit_open"
    assert calls == ["query"]


def test_wencai_breaker_reason_is_visible_in_slice_lineage(tmp_path):
    from engine_core.data_pipeline.providers.market_fallback_chain import WencaiMarketGate
    from engine_core.slices.market_fallbacks import MarketFallbackProviders

    gate = WencaiMarketGate(lambda *_: {"status_code": 403},
                            state_path=tmp_path / "wencai.json",
                            clock_fn=lambda: 1000.0, sleep_fn=lambda _: None)
    providers = MarketFallbackProviders(
        auction_td=lambda request: None,
        auction_redis=lambda request: None,
        wencai_gate=gate,
        coverage_threshold=1.0,
    )
    result = SliceReader(providers.providers()).get(
        "auction_snapshot", trade_date="2026-10-16", asof="2026-10-16T09:21:00+08:00",
        tag="0920",
    )

    attempt = next(item for item in result.meta["lineage"] if item["source"] == "wencai")
    assert attempt["status"] == "PROVIDER_UNAVAILABLE"
    assert attempt["reason"] == "provider_blocked"


def test_wencai_limit_state_includes_down_pool_as_auxiliary_evidence(tmp_path):
    from engine_core.data_pipeline.providers.market_fallback_chain import WencaiMarketGate
    from engine_core.slices.market_fallbacks import MarketFallbackProviders

    def fetch(query, loop, cap):
        if "跌停" in query:
            return [{"股票代码": "600002"}]
        return [{"股票代码": "600001", "涨停封单额": "10000"}]

    gate = WencaiMarketGate(fetch, state_path=tmp_path / "wencai.json",
                            clock_fn=lambda: 1000.0, sleep_fn=lambda _: None)
    providers = MarketFallbackProviders(
        limit_inputs=lambda request: None, limit_q2=lambda request: None,
        wencai_gate=gate, coverage_threshold=1.0,
    )
    result = SliceReader(providers.providers()).get(
        "limit_state", trade_date="2026-10-16", asof="2030-10-16T09:32:00+08:00",
        expected_count=2,
    )

    down = next(row for row in result.rows if row["symbol"] == "600002")
    assert down["limit_state"] == -1
    assert down["wencai_in_limit_down_pool"] is True
    assert down["state_evidence"] == "WENCAI_LIMIT_DOWN_POOL_MEMBERSHIP"
    assert result.meta["quality"]["limit_down_is_auxiliary"] is True


def test_redis_q2_anchor_is_price_only_for_0920_and_ln_is_never_a_limit_flag():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from engine_core.slices.market_fallbacks import MarketFallbackProviders

    class Pipeline:
        def __init__(self, redis): self.redis, self.keys = redis, []
        def hgetall(self, key): self.keys.append(key)
        def execute(self): return [self.redis.rows[key] for key in self.keys]

    class Redis:
        rows = {"q2:600001": {"a20": "10125", "am": "0", "ln": "1", "ls": "1",
                              "ts": "1792104000000"}}
        writes = []
        def get(self, key):
            assert key == "market:auction:anchor:20261016"
            return None
        def smembers(self, key): return {"600001"}
        def pipeline(self, transaction=False): return Pipeline(self)
        def __getattr__(self, name):
            if name.startswith(("set", "hset", "delete", "expire", "sadd")):
                self.writes.append(name)
                raise AssertionError("Redis writes are forbidden")
            raise AttributeError(name)

    redis = Redis()
    providers = MarketFallbackProviders(redis=redis)
    result = SliceReader(providers.providers()).get(
        "auction_snapshot", trade_date="2026-10-16",
        asof=datetime.now(ZoneInfo("Asia/Shanghai")).replace(year=2030).isoformat(), tag="0920",
    )
    assert result.meta["source"] == "legacy_redis_q2"
    assert result.rows[0]["match_price_milli"] == 10125
    assert result.rows[0]["match_amount_yuan"] is None
    limit_result = SliceReader(providers.providers()).get(
        "limit_state", trade_date="2026-10-16",
        asof=datetime.now(ZoneInfo("Asia/Shanghai")).replace(year=2030).isoformat(),
    )
    assert limit_result.rows[0]["limit_state"] == 1
    assert limit_result.rows[0]["legacy_ln_large_net_yuan"] == 1
    assert redis.writes == []
