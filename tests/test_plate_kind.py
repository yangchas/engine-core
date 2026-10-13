from engine_core.plate_kind import classify_plate_kind
from engine_core.plate_rules import is_valid_plate_candidate


def test_noise_examples_are_classified_without_changing_candidate_validity():
    names = ["320万元", "4月20日", "920万元至4", "企业71家", "71家", "募集资金总额不超过6亿元", "总投资额约574亿元"]
    assert [classify_plate_kind(name) for name in names] == ["NOISE"] * len(names)
    assert is_valid_plate_candidate("320万元") is True


def test_event_examples_are_classified():
    names = ["ST摘帽", "一季报增长", "业绩增长", "中报增长"]
    assert [classify_plate_kind(name) for name in names] == ["EVENT"] * len(names)


def test_theme_and_generic_examples():
    assert [classify_plate_kind(name) for name in ["人工智能", "低空经济", "AI应用", "一带一路", "先进封装"]] == ["THEME"] * 5
    assert classify_plate_kind("机器人") == "GENERIC"


def test_noise_and_event_rules_cover_common_shapes():
    assert classify_plate_kind("2026年公告") == "NOISE"
    assert classify_plate_kind("8.5亿元") == "NOISE"
    assert classify_plate_kind("涨幅12%概念") == "NOISE"
    assert classify_plate_kind("公司主营业务范围包括多个方向") == "NOISE"
    assert classify_plate_kind("回购") == "EVENT"

