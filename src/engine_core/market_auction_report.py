"""Deterministic market-level auction fact report.

A presentation boundary only: it renders already-computed facts (Q2 auction
summary, previous-day limit structure/feedback, theme auction deltas) as plain
text plus a dict.  It never fills a missing value, changes a unit, queries a
provider or applies a strategy threshold.  The overall status is the weakest
status of the supplied components, so a partial input is visible as PARTIAL.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping, Optional, Sequence, Tuple

from .contracts import canonical_hash
from .facts import FactStatus
from .market_summary import Q2AuctionSummaryProjection
from .previous_day_limit_feedback import PreviousDayLimitFeedbackFact
from .previous_day_limit_structure import PreviousDayLimitStructureFact
from .stock_extremes import StockExtremes
from .theme_auction_delta import ThemeAuctionDeltaFact

MARKET_REPORT_CONTRACT_VERSION = "MarketAuctionFactReportV1"
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ORDER = {
    FactStatus.READY: 0,
    FactStatus.PARTIAL: 1,
    FactStatus.MISSING: 2,
    FactStatus.UNAVAILABLE: 3,
    FactStatus.INVALID: 4,
}


@dataclass(frozen=True)
class MarketAuctionReport:
    trade_date: str
    event_id: str
    status: str
    component_status: Mapping[str, str]
    text: str
    content_hash: str

    def as_mapping(self) -> Mapping[str, Any]:
        return {
            "contract": MARKET_REPORT_CONTRACT_VERSION,
            "trade_date": self.trade_date,
            "event_id": self.event_id,
            "status": self.status,
            "component_status": dict(self.component_status),
            "content_hash": self.content_hash,
        }


def _show(value: Any) -> str:
    if value is None:
        return "unavailable"
    if isinstance(value, float):
        return ("%.4f" % value).rstrip("0").rstrip(".")
    return str(value)


def _status_value(status: Any) -> str:
    return getattr(status, "value", str(status))


def _theme_key(fact: ThemeAuctionDeltaFact, field: str):
    value = getattr(fact, field)
    return (value is None, -(value or 0.0), fact.theme_id)


def build_market_auction_report(
    *,
    trade_date: str,
    event_id: str,
    data_origin: str,
    market_summary: Optional[Q2AuctionSummaryProjection] = None,
    previous_limit_structure: Optional[PreviousDayLimitStructureFact] = None,
    previous_limit_feedback: Optional[PreviousDayLimitFeedbackFact] = None,
    theme_facts: Sequence[ThemeAuctionDeltaFact] = (),
    stock_extremes: Optional[StockExtremes] = None,
    top_n: int = 10,
    notes: Sequence[str] = (),
) -> MarketAuctionReport:
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

    components: dict = {}
    lines = [
        "# 竞价事实报告 %s %s" % (trade_date, event_id.strip()),
        "来源：%s" % data_origin.strip(),
    ]
    if market_summary is not None:
        components["market_summary"] = market_summary.status
        metrics = market_summary.metrics
        if market_summary.status in (FactStatus.UNAVAILABLE, FactStatus.MISSING, FactStatus.INVALID):
            # No observation: show every metric as unavailable, never as 0.
            metrics = {}
        lines += [
            "",
            "## 市场竞价摘要",
            "- status: %s" % _status_value(market_summary.status),
            "- stock_count: %s" % _show(metrics.get("stock_count")),
            "- valid_stock_count: %s" % _show(metrics.get("valid_stock_count")),
            "- unavailable_stock_count: %s" % _show(metrics.get("unavailable_stock_count")),
            "- positive/negative/flat: %s/%s/%s" % (
                _show(metrics.get("positive_count")),
                _show(metrics.get("negative_count")),
                _show(metrics.get("flat_count")),
            ),
            "- auction_amount_yuan: %s" % _show(metrics.get("auction_amount_yuan")),
            "- limit_up/limit_down: %s/%s" % (
                _show(metrics.get("limit_up_count")),
                _show(metrics.get("limit_down_count")),
            ),
            "- limit_up_seal_amount_yuan: %s" % _show(metrics.get("limit_up_seal_amount_yuan")),
            "- market_universe_coverage: %s" % market_summary.market_universe_coverage_status,
        ]
    if previous_limit_structure is not None:
        s = previous_limit_structure
        components["previous_limit_structure"] = s.status
        lines += [
            "",
            "## 昨日涨停结构",
            "- status: %s" % _status_value(s.status),
            "- previous_trade_date: %s" % _show(s.previous_trade_date),
            "- row_count: %s%s" % (
                _show(s.row_count),
                " (下限, 另有 %d 行被拒绝)" % s.excluded_row_count if s.excluded_row_count else "",
            ),
            "- highest_board_height: %s%s (%s)" % (
                "≥" if s.excluded_row_count else "",
                _show(s.highest_board_height),
                ",".join(s.highest_board_symbols) or "unavailable",
            ),
            "- board_height_distribution: %s" % _show(dict(s.board_height_distribution)),
        ]
    if previous_limit_feedback is not None:
        f = previous_limit_feedback
        components["previous_limit_feedback"] = f.status
        lines += [
            "",
            "## 昨日涨停今日竞价表现",
            "- status: %s" % _status_value(f.status),
            "- prior_limit_up_count: %s%s" % (
                _show(f.prior_limit_up_count),
                " (下限, 另有 %d 行被拒绝)" % f.excluded_row_count if f.excluded_row_count else "",
            ),
            "- valid_return/unavailable_return: %s/%s" % (
                _show(f.valid_return_count), _show(f.return_unavailable_count)
            ),
            "- up/down/flat: %s/%s/%s" % (_show(f.up_count), _show(f.down_count), _show(f.flat_count)),
            "- up_ratio: %s" % _show(f.up_ratio),
            "- median_return_pct: %s" % _show(f.median_return_pct),
        ]
    facts = tuple(theme_facts)
    if facts:
        ready = sum(1 for item in facts if item.status is FactStatus.READY)
        components["theme_delta"] = (
            FactStatus.READY if ready == len(facts) else FactStatus.PARTIAL
        )
        lines += [
            "",
            "## 主题竞价增量（0924→0925）",
            "- themes: %d (READY %d / 其他 %d)" % (len(facts), ready, len(facts) - ready),
        ]
        for title, field in (
            ("按竞价金额 Top%d" % top_n, "amount_yuan"),
            ("按金额增量 Top%d" % top_n, "amount_delta_yuan"),
        ):
            lines.append("### %s" % title)
            ranked = [item for item in sorted(facts, key=lambda x: _theme_key(x, field))
                      if getattr(item, field) is not None][:top_n]
            if not ranked:
                lines.append("- unavailable")
            for rank, item in enumerate(ranked, 1):
                lines.append(
                    "%d. %s | status=%s | n=%s | amount=%s | delta=%s | bid_delta=%s | pct_delta_avg=%s | ratio_avg=%s"
                    % (
                        rank, item.theme_id, _status_value(item.status), _show(item.symbol_count),
                        _show(item.amount_yuan), _show(item.amount_delta_yuan),
                        _show(item.bid_amount_delta_yuan), _show(item.change_pct_delta_avg),
                        _show(item.amount_ratio_avg),
                    )
                )
    if stock_extremes is not None:
        components["stock_extremes"] = stock_extremes.status
        lines += [
            "",
            "## 个股竞价极值（单一指标排序，无评分、无结论）",
            "- status: %s | candidates: %s" % (
                _status_value(stock_extremes.status), _show(stock_extremes.candidate_count)),
        ]
        titles = {
            "auction_amount_top": "竞价额",
            "auction_gain_top": "竞价涨幅(bp)",
            "auction_drop_top": "竞价跌幅(bp)",
            "limit_up_seal_top": "涨停封单额",
            "amount_delta_top": "竞价额增量(0924→0925)",
        }
        for key, rows in stock_extremes.rankings.items():
            excluded = stock_extremes.excluded_counts.get(key, 0)
            lines.append("### %s%s" % (
                titles.get(key, key), " (排除缺值 %d)" % excluded if excluded else ""))
            if not rows:
                lines.append("- unavailable")
            for row in rows:
                lines.append("%d. %s %s | %s %s" % (
                    row["rank"], row["symbol"], row.get("name") or "",
                    _show(row["value"]), stock_extremes.units.get(key, "")))
    if notes:
        lines += ["", "## 备注"] + ["- %s" % str(item) for item in notes]
    if components:
        worst = max(components.values(), key=lambda s: _ORDER.get(s, 9))
        overall = _status_value(worst)
    else:
        overall = _status_value(FactStatus.UNAVAILABLE)
    lines.insert(2, "整体状态：%s" % overall)
    text = "\n".join(lines) + "\n"
    component_status = {key: _status_value(value) for key, value in sorted(components.items())}
    return MarketAuctionReport(
        trade_date=trade_date,
        event_id=event_id.strip(),
        status=overall,
        component_status=component_status,
        text=text,
        content_hash=canonical_hash(
            {"contract": MARKET_REPORT_CONTRACT_VERSION, "text": text, "components": component_status}
        ),
    )


__all__ = [
    "MARKET_REPORT_CONTRACT_VERSION",
    "MarketAuctionReport",
    "build_market_auction_report",
]
