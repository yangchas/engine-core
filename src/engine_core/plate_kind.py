"""Classify plate labels for strategy-free aggregation output."""

from __future__ import annotations

import re

from .plate_rules import is_generic_plate


EVENT_KEYWORDS = (
    "业绩", "财报", "季报", "年报", "中报", "摘帽", "预增", "扭亏", "分红",
    "回购", "增持", "减持", "中标", "合同", "获批", "订单", "重组", "并购",
)

_NOISE_PATTERNS = (
    re.compile(r"\d+月\d+日"),
    re.compile(r"\d{4}年"),
    re.compile(r"\d+(?:\.\d+)?(?:万|亿)?元"),
    re.compile(r"\d+家|\d+只|\d+%"),
    re.compile(r"^(?:\d+(?:\.\d+)?|\d+(?:\.\d+)?\s*[:/比]\s*\d+(?:\.\d+)?)$"),
)
_SENTENCE_MARKERS = ("不超过", "约", "总投资", "募集")


def classify_plate_kind(name: str) -> str:
    """Return NOISE, EVENT, GENERIC, or THEME for a plate label."""
    text = str(name or "").strip()
    if (
        any(pattern.search(text) for pattern in _NOISE_PATTERNS)
        or len(text) > 12
        or any(marker in text for marker in _SENTENCE_MARKERS)
    ):
        return "NOISE"
    if any(keyword in text for keyword in EVENT_KEYWORDS):
        return "EVENT"
    if is_generic_plate(text):
        return "GENERIC"
    return "THEME"


__all__ = ["EVENT_KEYWORDS", "classify_plate_kind"]
