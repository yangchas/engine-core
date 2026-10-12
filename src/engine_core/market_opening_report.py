"""Deterministic market-level opening (09:32) fact report.

Presentation boundary over an already-computed opening evidence mapping (the
``OPENING_0932`` block of a replay/shadow result).  It never fills a missing
value, changes a unit, or applies a strategy threshold; symbols with a missing
metric are skipped from rankings and counted instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Optional, Sequence

from .contracts import canonical_hash

MARKET_OPENING_REPORT_CONTRACT_VERSION = "MarketOpeningFactReportV1"
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class MarketOpeningReport:
    trade_date: str
    event_id: str
    status: str
    text: str
    content_hash: str

    def as_mapping(self) -> Mapping[str, Any]:
        return {
            "contract": MARKET_OPENING_REPORT_CONTRACT_VERSION,
            "trade_date": self.trade_date,
            "event_id": self.event_id,
            "status": self.status,
            "content_hash": self.content_hash,
        }


def _show(value: Any) -> str:
    if value is None:
        return "unavailable"
    if isinstance(value, float):
        return ("%.4f" % value).rstrip("0").rstrip(".")
    return str(value)


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number == number and abs(number) != float("inf") else None


def _ranked(facts: Mapping[str, Mapping[str, Any]], field: str, *, top_n: int, descending: bool):
    values = []
    for symbol, fact in facts.items():
        number = _number(fact.get(field)) if isinstance(fact, Mapping) else None
        if number is not None:
            values.append((symbol, number))
    values.sort(key=lambda item: ((-item[1] if descending else item[1]), item[0]))
    return values[:top_n], len(values)


def build_market_opening_report(
    *,
    trade_date: str,
    event_id: str,
    data_origin: str,
    opening_evidence: Mapping[str, Any],
    top_n: int = 10,
    notes: Sequence[str] = (),
) -> MarketOpeningReport:
    if (
        not isinstance(trade_date, str)
        or not _DATE.fullmatch(trade_date)
        or date.fromisoformat(trade_date).isoformat() != trade_date
    ):
        raise ValueError("trade_date must use strict YYYY-MM-DD form")
    if not isinstance(event_id, str) or not event_id.strip():
        raise ValueError("event_id is required")
    if not isinstance(data_origin, str) or not data_origin.strip():
        raise ValueError("data_origin is required")
    if isinstance(top_n, bool) or not isinstance(top_n, int) or top_n < 1:
        raise ValueError("top_n must be a positive integer")
    if not isinstance(opening_evidence, Mapping):
        raise TypeError("opening_evidence must be a mapping")

    amount = opening_evidence.get("amount_2m_summary") or {}
    limit = opening_evidence.get("limit_state_summary") or {}
    section = opening_evidence.get("cross_section_facts") or {}
    breadth = section.get("market_breadth") or {}
    counts = limit.get("limit_state_counts") or {}
    facts = opening_evidence.get("facts_by_symbol") or {}
    field_status = opening_evidence.get("opening_fact_field_status_counts") or {}
    coverage = opening_evidence.get("coverage")
    complete = (
        amount.get("amount_2m_yuan_status") == "available"
        and limit.get("cohort_field_status") == "available"
        and isinstance(coverage, (int, float))
        and coverage >= 1.0
        and not opening_evidence.get("stale_symbol_count")
    )
    status = "READY" if complete else "PARTIAL"

    lines = [
        "# 开盘事实报告 %s %s" % (trade_date, event_id.strip()),
        "来源：%s" % data_origin.strip(),
        "整体状态：%s" % status,
        "",
        "## 覆盖",
        "- expected/observed/missing: %s/%s/%s" % (
            _show(opening_evidence.get("expected_symbol_count", amount.get("expected_count"))),
            _show(opening_evidence.get("observed_symbol_count")),
            _show(opening_evidence.get("missing_symbol_count")),
        ),
        "- coverage: %s" % _show(coverage),
        "- stale_symbol_count: %s" % _show(opening_evidence.get("stale_symbol_count")),
        "- full_market_coverage: %s" % _show(amount.get("full_market_coverage")),
        "- historical_available_at: %s" % _show(opening_evidence.get("availability_status")),
        "",
        "## 涨跌家数",
        "- up/down/flat/unknown: %s/%s/%s/%s" % (
            _show(breadth.get("up_count")), _show(breadth.get("down_count")),
            _show(breadth.get("flat_count")), _show(breadth.get("unknown_count")),
        ),
        "- breadth_includes_stale_observed_quotes: %s" % _show(
            section.get("breadth_includes_stale_observed_quotes")
        ),
        "",
        "## 涨跌停",
        "- up/down/normal: %s/%s/%s" % (
            _show(counts.get("up_count")), _show(counts.get("down_count")), _show(counts.get("normal_count"))
        ),
        "- present/valid/missing/invalid: %s/%s/%s/%s" % (
            _show(limit.get("limit_state_present_count")), _show(limit.get("limit_state_valid_count")),
            _show(limit.get("limit_state_missing_count")), _show(limit.get("limit_state_invalid_count")),
        ),
        "",
        "## 两分钟成交额",
        "- status: %s" % _show(amount.get("amount_2m_yuan_status")),
        "- amount_2m_yuan_sum: %s" % _show(amount.get("amount_2m_yuan_sum")),
        "- present/missing: %s/%s" % (
            _show(amount.get("amount_2m_yuan_present_count")),
            _show(amount.get("amount_2m_yuan_missing_count")),
        ),
        "- aggregation: %s" % _show(amount.get("aggregation")),
        "",
        "## 字段状态",
    ]
    for name in sorted(field_status):
        lines.append("- %s: %s" % (name, _show(dict(field_status[name]))))
    if isinstance(facts, Mapping) and facts:
        for title, field, descending in (
            ("两分钟成交额 Top%d" % top_n, "amount_2m_yuan", True),
            ("涨幅 Top%d" % top_n, "change_pct", True),
            ("跌幅 Top%d" % top_n, "change_pct", False),
        ):
            ranked, usable = _ranked(facts, field, top_n=top_n, descending=descending)
            lines += ["", "## %s（可用 %d / %d）" % (title, usable, len(facts))]
            if not ranked:
                lines.append("- unavailable")
            for rank, (symbol, value) in enumerate(ranked, 1):
                lines.append("%d. %s | %s=%s" % (rank, symbol, field, _show(value)))
    if notes:
        lines += ["", "## 备注"] + ["- %s" % str(item) for item in notes]
    text = "\n".join(lines) + "\n"
    return MarketOpeningReport(
        trade_date=trade_date,
        event_id=event_id.strip(),
        status=status,
        text=text,
        content_hash=canonical_hash(
            {"contract": MARKET_OPENING_REPORT_CONTRACT_VERSION, "text": text}
        ),
    )


__all__ = [
    "MARKET_OPENING_REPORT_CONTRACT_VERSION",
    "MarketOpeningReport",
    "build_market_opening_report",
]
