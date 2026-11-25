import pytest

from engine_core.plate_rules import (
    PLATE_RULES_VERSION,
    build_plate_candidates_from_reason,
    build_yest_limit_theme_candidates,
    choose_pool_primary_plate,
    choose_primary_plate,
    choose_runtime_primary_plate,
    collapse_runtime_primary_plate,
    explain_candidate,
    is_generic_plate,
    is_hard_generic_plate,
    is_valid_plate_candidate,
    merge_theme_lists,
    normalize_plate_name,
    order_runtime_theme_candidates,
    prioritize_core_themes,
    split_plate_tokens,
)


def test_version_and_normalization_examples():
    assert PLATE_RULES_VERSION == "PlateRulesV1"
    cases = [
        ("  人工智能概念  ", "人工智能"),
        ("芯片板块", "芯片"),
        ("低空经济题材", "低空经济"),
        ("机器人（产业链）", "机器人"),
        ("通信(光模块)", "通信"),
        ("一季报增长", "一季报增长"),
        ("一季度业绩预增", "一季报增长"),
        ("算力。公司主营业务介绍", "算力"),
        ("   ", ""),
        ("新能源", "新能源"),
    ]
    for value, expected in cases:
        assert normalize_plate_name(value) == expected


def test_candidate_explanation_reason_codes():
    cases = [
        ("", {"cleaned": "", "valid": False, "generic": True, "hard_generic": True, "reject_reason": "EMPTY"}),
        ("北京市", {"cleaned": "北京市", "valid": False, "generic": False, "hard_generic": False, "reject_reason": "REGION_ONLY"}),
        ("123.45", {"cleaned": "123", "valid": False, "generic": False, "hard_generic": False, "reject_reason": "PURE_NUMBER"}),
        ("这是一个很长很长的板块名称", {"cleaned": "这是一个很长很长的板块名称", "valid": False, "generic": False, "hard_generic": False, "reject_reason": "TOO_LONG"}),
        ("TOOLONG", {"cleaned": "TOOLONG", "valid": False, "generic": False, "hard_generic": False, "reject_reason": "ASCII_TOO_LONG"}),
        ("业绩同比增长", {"cleaned": "业绩同比增长", "valid": False, "generic": False, "hard_generic": False, "reject_reason": "ANNOUNCEMENT_KEYWORD"}),
        ("公司主营业务", {"cleaned": "公司主营业务", "valid": False, "generic": False, "hard_generic": False, "reject_reason": "INVALID_KEYWORD"}),
        ("AI", {"cleaned": "AI", "valid": True, "generic": False, "hard_generic": False, "reject_reason": None}),
        ("机器人", {"cleaned": "机器人", "valid": True, "generic": True, "hard_generic": False, "reject_reason": None}),
        ("国企改革", {"cleaned": "国企改革", "valid": True, "generic": True, "hard_generic": True, "reject_reason": None}),
    ]
    for value, expected in cases:
        assert explain_candidate(value) == expected


def test_generic_validity_and_token_splitting_examples():
    assert is_generic_plate("地方国企改革") is True
    assert is_generic_plate("MSCI中国") is True
    assert is_generic_plate("数字经济") is True
    assert is_generic_plate("机器人") is True
    assert is_generic_plate("人工智能") is False
    assert is_hard_generic_plate("昨日曾涨停") is True
    assert is_hard_generic_plate("机器人") is False
    assert is_valid_plate_candidate("AI") is True
    assert is_valid_plate_candidate("EDA") is False
    assert split_plate_tokens("芯片、通信/北京市", "算力+芯片") == ["芯片", "通信", "算力"]
    assert split_plate_tokens("概念公告", "一季报增长") == ["一季报增长"]


def test_merge_primary_and_theme_priority_examples():
    assert merge_theme_lists(["芯片,通信", "芯片"], ["人工智能", "北京市"]) == ["芯片", "通信", "人工智能"]
    assert choose_primary_plate(["昨日涨停", "人工智能", "机器人"]) == "人工智能"
    assert choose_primary_plate(["昨日涨停", "芯片", "机器人"]) == "芯片"
    assert choose_primary_plate(["昨日涨停"], fallback="通信概念") == "通信"
    assert choose_primary_plate(["北京市", "123"]) == ""
    assert prioritize_core_themes(["芯片", "通信", "机器人"], ["人工智能"], max_count=2) == ["芯片", "通信"]
    assert prioritize_core_themes(["北京市"], ["算力", "芯片"], max_count=2) == ["算力", "芯片"]
    assert prioritize_core_themes(["芯片"], ["芯片", "通信"], max_count=3) == ["芯片", "通信"]


def test_reason_and_pool_candidate_examples():
    assert build_plate_candidates_from_reason(reason="机器人；其他描述", group_str="芯片,通信") == ["芯片", "通信", "机器人"]
    assert build_plate_candidates_from_reason(reason="公告信息", group_str="", gnsm="算力产业") == ["算力产业"]
    assert build_plate_candidates_from_reason(reason="公告信息", group_str="", gnsm="这是一个超过二十四个字符的行业概念说明文本") == []
    assert choose_pool_primary_plate(["国企改革", "通信"], ["芯片"]) == "通信"
    assert choose_pool_primary_plate(["昨日涨停", "机器人"], ["通信"]) == "机器人"
    assert choose_pool_primary_plate(["国企改革"], ["芯片"]) == "芯片"
    assert choose_pool_primary_plate(["国企改革"], [], fallback="通信") == "通信"


def test_runtime_alias_fallback_and_theme_order_examples():
    assert collapse_runtime_primary_plate("玻璃基板") == "芯片"
    assert collapse_runtime_primary_plate("光纤") == "通信"
    assert collapse_runtime_primary_plate("通信设备") == "通信"
    assert choose_runtime_primary_plate(["先进封装", "通信"], fallback="芯片") == "芯片"
    assert choose_runtime_primary_plate(["先进封装", "通信"], fallback="通信") == "通信"
    assert choose_runtime_primary_plate(["国企改革", "机器人"], pool_plate="机器人") == "机器人"
    assert choose_runtime_primary_plate(["通信"], pool_plate="") == "通信"
    assert order_runtime_theme_candidates(["通信", "光纤", "芯片"], primary_plate="通信") == ["通信", "光纤", "芯片"]
    assert order_runtime_theme_candidates(["芯片", "通信"], primary_plate="光纤") == ["通信", "芯片"]
    assert build_yest_limit_theme_candidates(pool_plate="国企改革,通信", reason_candidates=["芯片"], existing_themes=["算力"]) == ["通信", "芯片", "国企改革", "算力"]
    assert build_yest_limit_theme_candidates(reason_candidates=["芯片", "通信"], existing_themes=["算力"]) == ["芯片", "通信"]
