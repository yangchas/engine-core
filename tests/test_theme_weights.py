from __future__ import annotations

import pytest

from engine_core import (
    GENERIC_THEME_DISCOUNT,
    SECONDARY_THEME_WEIGHT,
    is_generic_theme,
    normalize_theme_name,
    resolve_legacy_theme_weights,
    split_theme_tokens,
)


def test_theme_weights_match_legacy_primary_secondary_and_generic_discount():
    assert resolve_legacy_theme_weights(
        "国企改革",
        ("芯片,机器人", "国企改革"),
    ) == (
        ("芯片", 1.0),
        ("国企改革", SECONDARY_THEME_WEIGHT * GENERIC_THEME_DISCOUNT),
    )


def test_theme_weights_primary_plate_is_first_even_when_real_names_repeat_it():
    assert resolve_legacy_theme_weights("白酒", ("银行", "白酒", "地产链")) == (
        ("白酒", 1.0),
        ("银行", SECONDARY_THEME_WEIGHT),
    )


def test_theme_weights_keep_only_two_candidates_and_do_not_invent_missing_theme():
    assert resolve_legacy_theme_weights("", ("芯片", "通信", "机器人")) == (
        ("芯片", 1.0),
        ("通信", SECONDARY_THEME_WEIGHT),
    )
    assert resolve_legacy_theme_weights("", ()) == ()


def test_theme_token_normalization_matches_legacy_suffix_and_reason_rules():
    assert normalize_theme_name("一季度增长（概念）") == "一季报增长"
    assert split_theme_tokens("芯片/公司位于上海/银行（行业）") == ["芯片", "银行"]
    assert is_generic_theme("国企改革")
    assert not is_generic_theme("芯片")


def test_theme_weights_do_not_accept_non_finite_or_unordered_inputs_by_accident():
    # The function consumes an ordered iterable because the legacy consumer's
    # first two candidates are semantically meaningful.
    with pytest.raises(TypeError):
        resolve_legacy_theme_weights("芯片", None)  # type: ignore[arg-type]
