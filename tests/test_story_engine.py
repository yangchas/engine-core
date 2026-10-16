import json
import re
from datetime import date, timedelta

from engine_core.anomaly_detector import detect_anomalies
from engine_core.story_composer import compose_story
from engine_core.story_features import build_story_features, build_live_story_features
from engine_core.story_memory import append_memory, attach_next_day, make_daily_memory
from engine_core.tradability_funnel import filter_candidates
from engine_core.email_board import _story_view, _tiles, _extremes, _plates, _ladder, render_email_board
from engine_core.email_board import _story_text
from engine_core.email_board import _front_page_view


def test_missing_zero_and_small_sample_anomaly_policy():
    current = [{"metric_id": "x", "stage": "auction", "value": 0, "unit": "count"},
               {"metric_id": "missing", "stage": "auction", "value": None, "missing_reason": "not captured"}]
    history = [{"metric_id": "x", "stage": "auction", "value": n} for n in range(1, 6)]
    found = detect_anomalies(current, history)
    assert found[0]["kind"] == "OBSERVED_RANK"
    assert found[0]["n"] == 5
    assert found[1]["kind"] == "UNKNOWN"
    assert found[1]["value"] is None


def test_stage_projection_and_funnel_ignore_future_results():
    facts = {"trade_date": "2026-10-15", "auction_median_pct": -1.0,
             "close_median_pct": -3.0, "stage": "auction"}
    early = build_story_features(facts, stage="auction", cutoff="09:25",
                                 observed_at="2026-10-15T09:25:00+08:00", provenance={})
    poisoned = dict(facts, close_median_pct=99.0, next_day_winner="Winners")
    early_poisoned = build_story_features(poisoned, stage="auction", cutoff="09:25",
                                          observed_at="2026-10-15T09:25:00+08:00", provenance={})
    assert early == early_poisoned
    candidates = [{"symbol": "000001", "name": "甲", "bucket": "watch", "auction_pct": 1,
                   "close_pct": 10}, {"symbol": "000002", "name": "乙", "bucket": "wait",
                   "auction_pct": 2, "close_pct": -10}]
    one = filter_candidates(candidates, early, asof="09:25")
    two = filter_candidates([dict(candidates[0], close_pct=-99), dict(candidates[1], close_pct=99)],
                            early_poisoned, asof="09:25")
    assert one == two
    open_facts = {"trade_date": "2026-10-15", "p0945_median": {"value": -1.2, "unit": "pp",
                                                                    "event_cutoff": "09:44:58"},
                  "p0945_late_bucket": {"value": 9, "event_cutoff": "09:45:01"},
                  "close_median_pct": -3.0}
    open_a = build_story_features(open_facts, stage="intraday", cutoff="09:45:00",
                                  observed_at="2026-10-15T09:45:00+08:00", provenance={})
    open_b = build_story_features(dict(open_facts, close_median_pct=100.0, next_day_winner="胜者"),
                                  stage="intraday", cutoff="09:45:00",
                                  observed_at="2026-10-15T09:45:00+08:00", provenance={})
    assert open_a == open_b
    assert "p0945_median" in open_a["metrics"] and "p0945_late_bucket" not in open_a["metrics"]
    assert filter_candidates(candidates, open_a, asof="09:45") == filter_candidates(candidates, open_b,
                                                                                   asof="09:45")


def test_story_caps_unobserved_act_and_json_safety():
    bundle = build_story_features({"trade_date": "2026-10-15", "auction_up_share": 0.2},
                                  stage="auction", cutoff="09:25", observed_at=None,
                                  provenance={"source": "fixture"})
    story = compose_story(bundle, [], {"shown": {"watch": [], "wait": [], "exclude": []}},
                          previous_acts=[], templates=[])
    assert len(story["top_events"]) <= 3
    assert len(story["acts"]) <= 4
    assert story["acts"][-1]["status"] == "UNOBSERVED"
    json.dumps(story, allow_nan=False)


def test_memory_next_day_is_separate_and_append_is_opt_in(tmp_path):
    bundle = build_story_features({"trade_date": "2026-10-15"}, stage="close", cutoff="15:00",
                                  observed_at="2026-10-15T15:00:00+08:00", provenance={})
    story = compose_story(bundle, [], {"shown": {}}, previous_acts=[], templates=[])
    memory = make_daily_memory(story, bundle, input_refs=[], rule_version="test")
    outcome = attach_next_day("2026-10-15", [{"name": "甲", "result": "observed"}],
                              observed_at="2026-10-16T15:00:00+08:00")
    assert "next_day_ref" in memory and memory["next_day_ref"] is None
    assert outcome["memory_ref"] == "2026-10-15"
    assert memory["acts"] == story["acts"]
    assert not list(tmp_path.iterdir())
    path = append_memory(memory, directory=tmp_path)
    assert path.endswith("2026-10-15.json")
    assert json.loads(open(path, encoding="utf-8").read()) == memory
    for offset in range(129):
        record = dict(memory, trade_date=(date(2026, 1, 1) + timedelta(days=offset)).isoformat())
        append_memory(record, directory=tmp_path)
    assert len(list(tmp_path.glob("????-??-??.json"))) == 128


def test_anomaly_sample_cutoffs_mad_zero_equal_rank_and_denominator_policy():
    def run(n, value, denominator=None):
        row = {"metric_id": "breadth_share", "stage": "close", "value": value}
        if denominator is not None:
            row["denominator"] = denominator
        history = [{"metric_id": "breadth_share", "stage": "close", "value": 1.0}
                   for _ in range(n)]
        return detect_anomalies([row], history)[0]

    assert run(4, 1.0)["kind"] == "CONTEXT"
    assert run(5, 1.0)["kind"] == "OBSERVED_RANK"
    assert run(19, 1.0)["kind"] == "OBSERVED_RANK"
    tied = run(20, 1.0)
    assert tied["p"] == 0.5 and tied["robust_z"] is None
    assert run(20, 2.0)["kind"] == "TAIL"
    assert run(20, 2.0, denominator=4)["quality"] == ["SMALL_DENOMINATOR_RANK_ONLY"]
    assert run(20, 2.0, denominator=5)["kind"] == "TAIL"


def test_cutoff_excludes_late_final_and_funnel_deduplicates_without_outcome_ordering():
    facts = {"trade_date": "2026-10-15", "metrics": {
        "virtual_match_amount_yuan": {"value": 123, "unit": "yuan", "event_cutoff": "09:24:00"},
        "final_amount_yuan": {"value": 456, "unit": "yuan", "event_cutoff": "09:26:00"},
        "late_tick": {"value": 1, "event_cutoff": "not-a-time"}}}
    early = build_story_features(facts, stage="auction", cutoff="09:25:00",
                                 observed_at="2026-10-15T09:25:00+08:00", provenance={})
    assert "virtual_match_amount_yuan" in early["metrics"]
    assert "final_amount_yuan" not in early["metrics"]
    assert "late_tick" not in early["metrics"]
    rows = [{"symbol": "000001", "name": "甲", "bucket": "watch", "theme": "x",
             "price_status": "INDICATIVE", "condition": "观察下一截点"},
            {"symbol": "000001", "name": "甲", "bucket": "exclude", "theme": "y",
             "price_status": "FINAL", "condition": "等待重新核验", "risk": "晚到终价"}]
    funnel = filter_candidates(rows, early, asof="09:25:00")
    assert funnel["counts"] == {"watch": 0, "wait": 0, "exclude": 1}
    assert [x["name"] for x in funnel["shown"]["exclude"]] == ["甲"]


def test_story_text_is_human_readable_and_stitches_four_acts_with_transitions():
    report = {
        "auction_median_pct": -1.91, "auction_up_share": 97 / 5215,
        "auction_amount_yuan": 20204600380, "auction_prev_common_ratio": 1.71,
        "auction_n": 5215, "open_up_share": 295 / 5215,
        "p0940_up_share": 706 / 5215, "p0945_up_share": 551 / 5215,
        "close_up_share": 400 / 5215, "close_median_pct": -3.0006978,
        "p0945_median": -1.8012608,
        "top_events": [{"id": "close_up_share", "display_label": "历史异常",
                         "value": 0.0761519, "n": 64, "baseline": "64日固定池对照",
                         "numerator": 395, "denominator": 5187,
                         "historical_n": 64, "ranks": {"ascending": 1}}],
    }
    story, _, rendered = _story_view(report, "close", "2026-10-15")
    assert "一句话今日：" in rendered
    assert "和昨天比" in rendered or "和往常比" in rendered
    assert "含当日65日最低" in rendered
    assert "早盘回升没有保持到收盘" in rendered
    assert "下午连续路径缺失" in rendered
    assert "close_up_share" not in rendered
    assert re.search(r"\d+\.\d{7,}", rendered) is None
    assert all(len(act.get("evidence", [])) <= 3 for act in story["acts"])


def test_funnel_reasons_are_stock_specific_and_empty_watch_has_explanation():
    bundle = build_story_features({"trade_date": "2026-10-15"}, stage="close", cutoff="15:00",
                                  observed_at=None, provenance={})
    candidates = [
        {"symbol": "1", "name": "甲", "bucket": "wait", "position20_prior": 0.0,
         "auction_price_status": "FINAL", "p0945_pct": -3.8, "plate0945": -1.9,
         "relative0945": -1.9},
        {"symbol": "2", "name": "乙", "bucket": "wait", "position20_prior": 0.24,
         "auction_price_status": "INDICATIVE", "p0945_pct": -2.8, "plate0945": -3.0,
         "relative0945": 0.2},
    ]
    funnel = filter_candidates(candidates, bundle, asof="09:45")
    reasons = [row["condition"]["text"] for row in funnel["shown"]["wait"]]
    assert len(set(reasons)) == 2
    assert "终价" in " ".join(reasons)
    assert funnel["reason_counts"]
    story = {"stage": "close", "funnel": {"shown": funnel["shown"], "counts": funnel["counts"],
              "reason_counts": funnel["reason_counts"]}}
    rendered_report = {"story": story, "candidate_facts": candidates}
    _, _, rendered = _story_view(rendered_report, "close", "2026-10-15")
    candidate_lines = [line for line in rendered.splitlines() if line.startswith("要等：")]
    assert len(candidate_lines) == 2
    assert candidate_lines[0] != candidate_lines[1]
    assert "能做：暂无" in rendered and "主要原因" in rendered


def test_live_story_features_uses_stage_scoped_market_and_plate_facts():
    report = {
        "trade_date": "2026-10-15",
        "market_price_volume": {"asof": "2026-10-15T09:25:06", "phase": "AUCTION",
            "market_match_amount_yuan": 100, "median_pct_change": -1.2,
            "up_share_pct": 12.0, "top_plates_by_share": [{"name": "通信", "market_share_bp": 200}]},
        "auction_terminal_0925": {"asof": "2026-10-15T09:25:06", "market_match_amount_yuan": 100},
        "ladder_section": {"rows": [{"symbol": "000001", "p0925_pct": -2}]},
        "chapters": {"extremes_board": {"auction": {"rows": [{"symbol": "000001"}]}},
                     "plate_quadrants": {"rows": [{"name": "通信"}]}},
        "close_median_pct": 88,
    }
    features = build_live_story_features(report, stage="auction", cutoff="09:26:00")
    assert features["metrics"]["auction_amount_yuan"]["value"] == 100
    assert features["metrics"]["auction_median_pct"]["value"] == -1.2
    assert features["metrics"]["plate_top_share"]["value"][0]["name"] == "通信"
    assert "close_median_pct" not in features["metrics"]


def test_funnel_does_not_repeat_condition_as_reason():
    bundle = build_story_features({"trade_date": "2026-10-15"}, stage="auction", cutoff="09:25",
                                  observed_at=None, provenance={})
    funnel = filter_candidates([{"symbol": "000001", "name": "甲", "bucket": "wait",
                                 "condition": "等下一截点确认", "risk": "回落"}], bundle,
                               asof="09:25")
    row = funnel["shown"]["wait"][0]
    assert row["reasons"][0]["text"] != row["condition"]["text"]
    assert row["filter_reasons"] == ["价格或成交数据缺失"]
    report = {"story": {"stage": "auction", "funnel": {"shown": funnel["shown"],
              "counts": funnel["counts"], "reason_counts": funnel["reason_counts"]}}}
    _, _, rendered = _story_view(report, "auction", "2026-10-15")
    candidate_line = next(line for line in rendered.splitlines() if line.startswith("要等："))
    assert candidate_line.count("等下一截点确认") == 1


def test_live_story_features_excludes_later_open_checkpoints_and_close():
    report = {
        "trade_date": "2026-10-15",
        "market_price_volume": {"asof": "2026-10-15T09:32:00", "phase": "OPEN",
            "status": "FRESH", "symbol_count": 5000, "market_match_amount_yuan": 100,
            "median_pct_change": -0.5, "up_share_pct": 25},
        "snapshots": [
            {"label": "0932", "asof": "2026-10-15T09:32:00", "median_pct": -0.5},
            {"label": "0938", "asof": "2026-10-15T09:38:00", "median_pct": 2.0},
            {"label": "0945", "asof": "2026-10-15T09:45:00", "median_pct": 4.0},
        ],
        "close_median_pct": 88,
    }
    features = build_live_story_features(report, stage="open", cutoff="09:32:00")
    metrics = features["metrics"]
    assert metrics["open_median_pct"]["value"] == -0.5
    assert metrics["p0932_median_pct"]["value"] == -0.5
    assert "p0938_median_pct" not in metrics
    assert "p0945_median_pct" not in metrics
    assert "close_median_pct" not in metrics


def test_open_story_compares_auction_to_opening_path_and_keeps_missing_explicit():
    report = {
        "trade_date": "2026-10-15",
        "market_price_volume": {"asof": "2026-10-15T09:45:00", "phase": "OPEN",
            "status": "FRESH", "symbol_count": 5000, "median_pct_change": -1.8,
            "up_share_pct": 9.9},
        "snapshots": [
            {"label": label, "asof": f"2026-10-15T{clock}:00", "median_pct_change": median,
             "up_share_pct": share}
            for label, clock, median, share in (("0925", "09:25", -1.92, 1.9),
                ("0932", "09:32", -1.2, 5.7), ("0938", "09:38", -0.7, 12.0),
                ("0945", "09:45", -1.8, 9.9))],
        "handoff": {"auction_0925": {"final_0925": {"status": "READY", "symbol_count": 5000,
            "asof": "09:25:06", "median_pct_change": -1.92, "up_share_pct": 1.9}}}}
    features = build_live_story_features(report, stage="open", cutoff="09:45:00")
    story = compose_story(features, [], {"shown": {}}, previous_acts=[])
    text = "\n".join(_story_text(story, report))
    assert "1.9%" in text and "5.7%" in text and "12.0%" in text and "9.9%" in text
    assert "改善但仍是少数个股上涨" in text
    assert "第三、四幕未到" in text


def test_auction_events_are_presented_as_three_observation_items():
    from engine_core.email_board import _story_text
    story = {"stage": "auction", "top_events": [
        {"id": "a", "display_label": "对照观察", "summary": "竞价额与基线。"},
        {"id": "b", "display_label": "历史异常", "summary": "上涨占比排名。"},
        {"id": "c", "display_label": "对照观察", "summary": "昨高标承接。"}],
        "acts": [], "funnel": {"shown": {}}}
    text = "\n".join(_story_text(story, {}))
    assert "今日3件不一样的事" in text
    assert all(value in text for value in ("竞价额与基线", "上涨占比排名", "昨高标承接"))


def test_funnel_uses_filter_reasons_not_candidate_strength_descriptions():
    features = build_story_features({"trade_date": "2026-10-15"}, stage="auction",
        cutoff="09:25", observed_at=None, provenance={})
    funnel = filter_candidates([{"symbol": "000001", "name": "甲", "bucket": "wait",
        "selection_reason": "板块竞价额第2；竞价较板块+0.9pp", "price_status": "INDICATIVE",
        "auction_price_status": "INDICATIVE", "match_amount_yuan": 20_000_000}], features,
        asof="09:25")
    row = funnel["shown"]["wait"][0]
    assert "板块竞价额第2" in " ".join(reason["text"] for reason in row["reasons"])
    assert "板块竞价额第2" not in " ".join(row["filter_reasons"])
    assert "终价" in row["condition"]["text"]
    assert funnel["reason_counts"] == {"竞价价仍为指示价，终价未确认": 1}


def test_live_story_degrades_for_absent_and_partial_auction_inputs():
    for report, cutoff in (({"trade_date": "2026-10-15"}, "09:27:00"),
        ({"trade_date": "2026-10-15", "snapshots": [
            {"label": "0920", "asof": "09:20:00", "up_share_pct": 20},
            {"label": "0924", "asof": "09:24:00", "up_share_pct": 30}]}, "09:27:00"),
        ({"trade_date": "2026-10-15", "auction_terminal_0925": {"status": "PARTIAL",
            "symbol_count": 4000, "expected_symbol_count": 5200, "asof": "09:25:06",
            "up_share_pct": 10}, "degraded": ["深市终价仍在到达"]}, "09:27:00")):
        features = build_live_story_features(report, stage="auction", cutoff=cutoff)
        story = compose_story(features, [], {"shown": {}}, previous_acts=[])
        text = "\n".join(_story_text(story, report))
        assert "口径与缺口" in text
        if "auction_terminal_0925" not in report:
            assert "缺少" in text or "没有可用事实" in text
        else:
            assert "PARTIAL" in text or "到达" in text


def test_empty_stale_funnel_reports_unknown_candidate_counts():
    report = {"trade_date": "2026-10-15", "stage": "auction",
              "chapters": {"market_price_volume": {"status": "STALE", "symbol_count": None}},
              "candidate_facts": [], "story": {"stage": "auction", "funnel": {"shown": {}, "counts": {}}}}
    rendered = "\n".join(_story_text(report["story"], report))
    assert "候选数量和过滤原因未知" in rendered
    assert "候选0只" not in rendered


def test_stale_zero_snapshot_is_missing_not_zero_in_story_and_tiles():
    from engine_core.email_board import _tiles
    report = {"trade_date": "2026-10-15", "snapshots": [{"label": "0925",
        "asof": "09:25:06", "status": "STALE", "symbol_count": 0,
        "market_match_amount_yuan": 0, "median_pct_change": 0, "up_share_pct": 0}],
        "chapters": {"market_price_volume": {"status": "STALE", "symbol_count": 0,
            "market_match_amount_yuan": 0, "median_pct_change": None, "up_share_pct": None}}}
    features = build_live_story_features(report, stage="auction", cutoff="09:26:00")
    assert features["metrics"]["auction_amount_yuan"]["value"] is None
    assert features["metrics"]["auction_median_pct"]["value"] is None
    assert "竞价匹配额" not in " ".join(label for label, _ in _tiles(report, "auction"))


def test_live_story_feature_rows_and_market_cutoff_fail_closed():
    report = {"trade_date": "2026-10-15",
        "market_price_volume": {"asof": "2026-10-15T09:38:30", "phase": "OPEN",
            "status": "FRESH", "symbol_count": 5000, "median_pct_change": 7.0},
        "ladder_section": {"rows": [{"symbol": "000001", "p0932_pct": 1.0,
            "p0940_pct": 9.0, "close_pct": 20.0}]},
        "chapters": {"extremes_board": {"open": {"rows": [{"p0932_pct": 1.0,
            "p0945_pct": 9.0, "close_pct": 20.0}]}}}}
    features = build_live_story_features(report, stage="open", cutoff="09:32:00")
    assert "open_median_pct" not in features["metrics"]
    ladder = features["metrics"]["ladder_rows"]["value"][0]
    extreme = features["metrics"]["extremes_rows"]["value"][0]
    assert ladder == {"symbol": "000001", "p0932_pct": 1.0}
    assert extreme == {"p0932_pct": 1.0}


def test_open_story_keeps_auction_handoff_when_open_slice_has_no_terminal():
    report = {"trade_date": "2026-10-15",
        "handoff": {"auction_0925": {"final_0925": {"asof": "2026-10-15T09:25:06",
            "status": "FRESH", "symbol_count": 5226, "market_match_amount_yuan": 200,
            "median_pct_change": -1.0, "up_share_pct": 10}}},
        "market_price_volume": {"asof": "2026-10-15T09:32:00", "phase": "OPEN",
            "status": "STALE", "symbol_count": 0}}
    features = build_live_story_features(report, stage="open", cutoff="09:32:00")
    assert features["metrics"]["auction_amount_yuan"]["value"] == 200
    assert features["metrics"]["auction_median_pct"]["value"] == -1.0
    assert features["metrics"]["open_median_pct"]["value"] is None


def test_live_report_chapter_shapes_feed_emotion_extremes_and_plate_appendices():
    report = {"chapters": {
        "market_price_volume": {"median_pct_change": -1.4, "up_share_pct": 20,
            "market_match_amount_yuan": 5_000_000_000, "limit_up": 3, "limit_down": 4,
            "top_plates_by_share": [{"name": "通信", "member_count": 80,
                "match_amount_yuan": 2_000_000_000, "market_share_bp": 400,
                "equal_weight_pct": -0.7}]},
        "yesterday_highboards_summary": {"yesterday_limit_pool": {"count": 40,
            "mean_pct_change": 1.2}},
        "extremes_board": {"auction": [{"symbol": "000001", "name": "甲",
            "auction_pct": -4.0, "auction_amount_yuan": 4_000_000_000,
            "selection_reason": "amount_top20"}]},
        "plate_quadrants": {"rows": [{"name": "芯片", "member_count": 100,
            "amount_yuan": 3_000_000_000, "our_pct": -0.4, "quadrant": "双负"}]},
    }}
    assert {label for label, _ in _tiles(report, "auction")} >= {"竞价中位", "上涨占比", "涨/跌停价代理", "竞价匹配额"}
    extreme_html, extreme_text = _extremes(report, "auction")
    assert "甲" in extreme_html and "甲" in extreme_text
    plate_html, plate_text = _plates(report, "auction")
    assert "通信" in plate_html and "通信" in plate_text
    open_html, open_text = _plates(report, "open")
    assert "芯片" in open_html and "芯片" in open_text and "收盘" not in open_text


def test_auction_story_headline_includes_available_historical_amount_baseline():
    features = build_story_features({"trade_date": "2026-10-15", "metrics": {
        "auction_amount_yuan": {"value": 202e8, "unit": "yuan"},
        "auction_median_pct": {"value": -1.9, "unit": "pp"},
        "auction_baseline_median_yuan": {"value": 169e8, "unit": "yuan"},
        "auction_baseline_multiple": {"value": 1.20, "unit": "ratio"},
        "auction_baseline_percentile": {"value": 67.2, "unit": "percentile"}},
        }, stage="auction", cutoff="09:26", observed_at=None, provenance={})
    story = compose_story(features, [], {"shown": {}}, previous_acts=[])
    assert "64日同刻中位基线169.0亿" in story["headline"]["text"]
    assert "1.20倍、67.2分位" in story["headline"]["text"]


def test_live_ladder_labels_yesterday_height_without_inventing_promotion():
    report = {"ladder_section": {"rows": [
        {"symbol": "000001", "name": "甲", "prior_streak": 3, "p0925_pct": 2.0},
        {"symbol": "000002", "name": "乙", "prior_streak": 3, "p0925_pct": -1.0},
    ], "promotion_by_previous_level": {}, "promotion_status": "READY"}}
    _, rendered = _ladder(report, "auction")
    assert "昨3板" in rendered and "当前样本2只" in rendered
    assert "晋" not in rendered


def test_open_plate_appendix_does_not_repeat_one_snapshot_as_a_trend():
    report = {"chapters": {"plate_quadrants": {"rows": [
        {"name": "通信", "member_count": 80, "amount_yuan": 3e9,
         "our_pct": -0.4, "quadrant": "双负"},
    ]}}}
    _, rendered = _plates(report, "open")
    assert "通信" in rendered
    assert "▄" not in rendered and "▁" not in rendered


def test_front_page_uses_em_dash_for_missing_and_shows_breadth_rank_inline():
    report = {
        "auction_terminal_0925": {"market_match_amount_yuan": 138e8,
            "median_pct_change": 1.2, "up_share_pct": 90.4,
            "auction_baseline_comparison": {"multiple": 0.81, "percentile": 23}},
        "chapters": {"yesterday_highboards_summary": {"yesterday_limit_pool": {
            "mean_pct_change": -3.0, "up_share_pct": 7.6}}},
        "front_page": {"ladder": {"clusters": [{"name": "冰雪产业", "share_bp": None,
            "delta_share_bp": None, "equal_weight_pct": 1.0, "judgement": "分化",
            "paths": [{"symbol": "000001", "name": "甲", "streak": 2,
                       "0925": 1.0, "0932": None, "0938": None, "0945": None}]}],
            "first_board_summary": "昨首板28只：高开16/低开11/封停价代理3"},
            "plate_strength": {"up": [], "down": [], "message": "缺0924"}},
        "story": {"top_events": [
            {"id": "auction_amount_context", "summary": "竞价额138亿"},
            {"id": "auction_breadth_context", "summary": "昨日收盘中位-3.0%/上涨7.6%→今日竞价+1.2%/90.4%；上涨占比近64日由高到低第1/64"}],
            "funnel": {"wait": [{"name": "甲", "condition": "观察0932价格是否保持并相对板块转强，确认后继续观察",
                                "risk": "高标波动与竞价承接不足，注意回落"}]}}}
    _, text = _front_page_view(report, "auction")
    assert "—Δ—" in text
    assert "历史分位见附表" not in text
    assert "昨日收盘中位-3.0%/上涨7.6%→今日竞价+1.2%/90.4%" in text and "近64日由高到低第1/64" in text
    assert "?" not in text and "…" not in text
    assert "要等 甲｜触发:开盘看承接｜险:高标波动" in text


def test_temperature_plain_text_does_not_repeat_tile_labels():
    _, plain = render_email_board({"market": {"auction_median": -1.2,
        "up_share_pct": 12.0, "market_match_amount_yuan": 100}}, "auction", "2026-10-15")
    assert "竞价中位 -1.2%" in plain
    assert "竞价中位 竞价中位" not in plain
