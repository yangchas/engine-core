"""Pure extraction of the legacy snapshot-to-theme weight contract.

The deployed ``engine_next`` auction consumer derives at most two theme
contributions from a stock snapshot.  This module keeps that data-shaping
behavior separate from theme aggregation and strategy labels:

* the first token of ``plate`` is the primary candidate;
* ``real_plate_names`` contributes additional candidates in order;
* a generic primary is swapped behind a non-generic secondary;
* weights are ``1.0`` and ``0.6`` and generic names are discounted by
  ``0.18``.

It is intentionally provider-free.  Redis mapping discovery and historical
availability remain outside this wheel.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Tuple


LEGACY_THEME_WEIGHTS_CONTRACT_VERSION = "LegacyThemeWeightsV1"
GENERIC_THEME_DISCOUNT = 0.18
SECONDARY_THEME_WEIGHT = 0.6

_GENERIC_THEME_NAMES = frozenset(
    {
        "国企改革",
        "地方国企改革",
        "央企改革",
        "国企",
        "央企",
        "中字头",
        "融资融券",
        "转融券",
        "昨日涨停",
        "昨日曾涨停",
        "昨日首板",
        "证金持股",
        "汇金持股",
        "MSCI中国",
        "沪股通",
        "深股通",
        "数字经济",
        "机器人",
        "金融",
        "地产链",
        "金融概念",
    }
)
_GENERIC_THEME_KEYWORDS = (
    "国企",
    "央企",
    "融资融券",
    "转融券",
    "昨日涨停",
    "昨日首板",
    "昨日曾涨停",
    "证金持股",
    "汇金持股",
    "MSCI",
    "沪股通",
    "深股通",
)
_INVALID_THEME_KEYWORDS = (
    "公司位于",
    "主营业务",
    "主要为",
    "生产和销售",
    "研发",
    "客户提供",
    "主要产品",
    "产品包括",
    "产品应用于",
    "贴牌加工",
    "知名品牌",
    "公司已投",
    "互动易",
    "投资者关系",
    "招股说明书",
    "半年报",
    "三季报",
    "年报",
    "月日",
)
_REGION_ONLY_NAMES = frozenset(
    {
        "北京市",
        "上海市",
        "天津市",
        "重庆市",
        "河北省",
        "山西省",
        "辽宁省",
        "吉林省",
        "黑龙江省",
        "江苏省",
        "浙江省",
        "安徽省",
        "福建省",
        "江西省",
        "山东省",
        "河南省",
        "湖北省",
        "湖南省",
        "广东省",
        "海南省",
        "四川省",
        "贵州省",
        "云南省",
        "陕西省",
        "甘肃省",
        "青海省",
        "台湾省",
        "内蒙古",
        "广西",
        "西藏",
        "宁夏",
        "新疆",
        "深圳市",
        "广州市",
        "杭州市",
        "苏州市",
        "南京市",
    }
)
_SPLIT_PATTERN = re.compile(r"[+,，、/|；;]+")
_REASON_TAIL_PATTERN = re.compile(r"[。.].*$")
_TRAILING_BRACKET_DETAIL_PATTERN = re.compile(r"([（(].*[）)])$")
_PURE_NUMBER_PATTERN = re.compile(r"^\d+(?:\.\d+)?$")


def normalize_theme_name(value: Any) -> str:
    """Normalize one legacy theme token without inventing a theme."""

    text = str(value or "").strip()
    if not text:
        return ""
    text = _REASON_TAIL_PATTERN.sub("", text)
    if _TRAILING_BRACKET_DETAIL_PATTERN.search(text):
        head = re.split(r"[（(]", text, maxsplit=1)[0].strip()
        if head:
            text = head
    for suffix in ("概念", "板块", "题材"):
        if text.endswith(suffix) and len(text) > len(suffix) + 1:
            text = text[: -len(suffix)]
    if "一季报" in text or "一季度" in text:
        return "一季报增长"
    return text.strip()


def is_generic_theme(name: str) -> bool:
    """Return the legacy generic-theme classification."""

    cleaned = normalize_theme_name(name)
    if not cleaned:
        return True
    if cleaned in _GENERIC_THEME_NAMES:
        return True
    return any(keyword in cleaned for keyword in _GENERIC_THEME_KEYWORDS)


def _is_valid_theme_candidate(name: str) -> bool:
    cleaned = normalize_theme_name(name)
    if not cleaned:
        return False
    if cleaned in _REGION_ONLY_NAMES or _PURE_NUMBER_PATTERN.fullmatch(cleaned):
        return False
    if len(cleaned) > 12 or (cleaned.isascii() and len(cleaned) > 2):
        return False
    if any(keyword in cleaned for keyword in ("公告", "同比", "减亏", "上年增长", "晚")):
        return False
    return not any(keyword in cleaned for keyword in _INVALID_THEME_KEYWORDS)


def split_theme_tokens(*values: Any) -> list[str]:
    """Split and validate theme names using the legacy token rules."""

    tokens: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        for part in _SPLIT_PATTERN.split(text):
            cleaned = normalize_theme_name(part)
            if not cleaned or not _is_valid_theme_candidate(cleaned) or cleaned in tokens:
                continue
            tokens.append(cleaned)
    return tokens


def resolve_legacy_theme_weights(
    plate: Any,
    real_plate_names: Iterable[Any] = (),
) -> Tuple[Tuple[str, float], ...]:
    """Resolve the legacy two-theme weighted contribution for one snapshot.

    The function is a pure data transform.  It does not fetch mappings, apply
    strategy thresholds, infer theme strength, or claim capital flow.
    """

    candidates: list[str] = []
    seen: set[str] = set()
    primary_tokens = split_theme_tokens(plate)
    primary = primary_tokens[0] if primary_tokens else ""
    for raw in real_plate_names:
        for token in split_theme_tokens(raw):
            if token in seen:
                continue
            seen.add(token)
            candidates.append(token)
    if primary and primary not in seen:
        candidates.insert(0, primary)
        seen.add(primary)
    elif primary:
        candidates = [primary] + [name for name in candidates if name != primary]
    if not candidates:
        return ()
    if len(candidates) >= 2 and is_generic_theme(candidates[0]) and not is_generic_theme(candidates[1]):
        candidates = [candidates[1], candidates[0], *candidates[2:]]
    weights: list[tuple[str, float]] = []
    for index, theme_id in enumerate(candidates[:2]):
        weight = 1.0 if index == 0 else SECONDARY_THEME_WEIGHT
        if is_generic_theme(theme_id):
            weight *= GENERIC_THEME_DISCOUNT
        weights.append((theme_id, weight))
    return tuple(weights)


__all__ = [
    "GENERIC_THEME_DISCOUNT",
    "LEGACY_THEME_WEIGHTS_CONTRACT_VERSION",
    "SECONDARY_THEME_WEIGHT",
    "is_generic_theme",
    "normalize_theme_name",
    "resolve_legacy_theme_weights",
    "split_theme_tokens",
]
