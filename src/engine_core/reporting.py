"""Build-only projection of frozen Core facts.

This module is deliberately narrower than the legacy email report.  It accepts
an already-frozen :class:`AuctionFactShadow` and produces a deterministic,
fact-only artifact.  It does not read Redis/TDengine, perform recovery, claim a
delivery slot, send SMTP/webhooks, or emit a strategy conclusion.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import re
from typing import Any, Mapping, Optional, Tuple

from .auction_shadow import AuctionFactShadow
from .contracts import (
    EVIDENCE_HASH_CONTRACT_VERSION,
    SEMANTIC_HASH_CONTRACT_VERSION,
    deep_freeze,
    evidence_hash,
    semantic_hash,
)
from .facts import FactStatus
from .market_summary import AuctionMarketSummaryFact


REPORT_CONTRACT_VERSION = "AuctionFactReportV1"
ALLOWED_DATA_ORIGINS = frozenset(
    {"production_capture", "replay_fixture_only", "current_cache_only"}
)
_STRICT_TRADE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _report_status(status: FactStatus) -> str:
    if status is FactStatus.READY:
        return "COMPLETE"
    if status is FactStatus.PARTIAL:
        return "PARTIAL"
    return "DATA_UNAVAILABLE"


def _display(value: Any) -> str:
    if value is None:
        return "unavailable"
    return str(value)


@dataclass(frozen=True)
class AuctionFactReportArtifact:
    """Immutable build-only report artifact for one auction fact evaluation."""

    report_id: str
    trade_date: str
    event_id: str
    data_origin: str
    status: str
    fact_status: FactStatus
    metrics: Mapping[str, Any]
    changes: Mapping[str, str]
    reason_codes: Tuple[str, ...]
    market_summary: Optional[AuctionMarketSummaryFact]
    provenance: Mapping[str, Any]
    text_body: str
    semantic_hash: str
    evidence_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "metrics", deep_freeze(self.metrics))
        object.__setattr__(self, "changes", deep_freeze(self.changes))
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))
        object.__setattr__(self, "provenance", deep_freeze(self.provenance))

    @property
    def format(self) -> str:
        return REPORT_CONTRACT_VERSION

    def as_mapping(self) -> Mapping[str, Any]:
        """Return the stable structured representation without side effects."""

        return {
            "format": self.format,
            "report_id": self.report_id,
            "trade_date": self.trade_date,
            "event_id": self.event_id,
            "data_origin": self.data_origin,
            "status": self.status,
            "fact_status": self.fact_status,
            "metrics": self.metrics,
            "changes": self.changes,
            "reason_codes": self.reason_codes,
            "market_summary": (
                self.market_summary.as_mapping()
                if self.market_summary is not None else None
            ),
            "provenance": self.provenance,
            "semantic_hash": self.semantic_hash,
            "evidence_hash": self.evidence_hash,
        }


def build_auction_fact_report(
    fact: AuctionFactShadow,
    *,
    trade_date: str,
    event_id: str,
    data_origin: str,
    market_summary: Optional[AuctionMarketSummaryFact] = None,
    source_time_min_ms: Optional[int] = None,
    source_time_max_ms: Optional[int] = None,
) -> AuctionFactReportArtifact:
    """Project a frozen auction fact into a deterministic report artifact.

    The function is intentionally a presentation boundary only.  The supplied
    fact must already have been computed; this function never fills missing
    values, changes units, queries a provider, or applies a strategy threshold.
    ``source_time_*`` belongs to provenance/evidence and therefore does not
    change the semantic report identity.
    """

    if not isinstance(fact, AuctionFactShadow):
        raise TypeError("fact must be an AuctionFactShadow")
    if not isinstance(fact.status, FactStatus):
        raise TypeError("fact.status must be a FactStatus")
    if market_summary is not None and not isinstance(
        market_summary, AuctionMarketSummaryFact
    ):
        raise TypeError("market_summary must be an AuctionMarketSummaryFact")
    if (
        not isinstance(trade_date, str)
        or not _STRICT_TRADE_DATE.fullmatch(trade_date)
        or date.fromisoformat(trade_date).isoformat() != trade_date
    ):
        raise ValueError("trade_date must use strict YYYY-MM-DD form")
    if not isinstance(event_id, str) or not event_id.strip():
        raise ValueError("event_id is required")
    event_id = event_id.strip()
    if data_origin not in ALLOWED_DATA_ORIGINS:
        raise ValueError("unsupported auction fact report data_origin")
    for field_name, value in (
        ("source_time_min_ms", source_time_min_ms),
        ("source_time_max_ms", source_time_max_ms),
    ):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
        ):
            raise ValueError(f"{field_name} must be a positive epoch-millisecond integer")
    if (
        source_time_min_ms is not None
        and source_time_max_ms is not None
        and source_time_min_ms > source_time_max_ms
    ):
        raise ValueError("source time range is inverted")

    report_id = f"auction-fact:{trade_date}:{event_id}"
    status = _report_status(fact.status)
    semantic_payload = {
        "contract_version": REPORT_CONTRACT_VERSION,
        "report_id": report_id,
        "trade_date": trade_date,
        "event_id": event_id,
        "status": status,
        "fact_status": fact.status,
        "metrics": fact.metrics,
        "changes": fact.changes,
        "reason_codes": fact.reason_codes,
        "market_summary": (
            market_summary.content_hash if market_summary is not None else None
        ),
    }
    provenance = {
        "data_origin": data_origin,
        "fact_content_hash": fact.content_hash,
        "fact_evidence_hash": fact.evidence_hash,
        "comparison_hash": fact.comparison_hash,
        "market_summary_content_hash": (
            market_summary.content_hash if market_summary is not None else None
        ),
        "market_summary_evidence_hash": (
            market_summary.evidence_hash if market_summary is not None else None
        ),
        "evidence_refs": fact.evidence_refs,
        "source_time_min_ms": source_time_min_ms,
        "source_time_max_ms": source_time_max_ms,
        "hash_contract_versions": {
            "semantic": SEMANTIC_HASH_CONTRACT_VERSION,
            "evidence": EVIDENCE_HASH_CONTRACT_VERSION,
        },
    }
    evidence_payload = {
        "contract_version": REPORT_CONTRACT_VERSION,
        "report_id": report_id,
        "data_origin": data_origin,
        "fact_content_hash": fact.content_hash,
        "fact_evidence_hash": fact.evidence_hash,
        "comparison_hash": fact.comparison_hash,
        "evidence_refs": fact.evidence_refs,
        "market_summary_evidence_hash": (
            market_summary.evidence_hash if market_summary is not None else None
        ),
        "market_summary_evidence_refs": (
            market_summary.evidence_refs if market_summary is not None else ()
        ),
        "source_time_min_ms": source_time_min_ms,
        "source_time_max_ms": source_time_max_ms,
    }
    text_body = _render_text(
        trade_date=trade_date,
        event_id=event_id,
        data_origin=data_origin,
        status=status,
        fact_status=fact.status,
        metrics=fact.metrics,
        changes=fact.changes,
        reason_codes=fact.reason_codes,
        market_summary=market_summary,
    )
    return AuctionFactReportArtifact(
        report_id=report_id,
        trade_date=trade_date,
        event_id=event_id,
        data_origin=data_origin,
        status=status,
        fact_status=fact.status,
        metrics=semantic_payload["metrics"],
        changes=semantic_payload["changes"],
        reason_codes=semantic_payload["reason_codes"],
        market_summary=market_summary,
        provenance=provenance,
        text_body=text_body,
        semantic_hash=semantic_hash(semantic_payload),
        evidence_hash=evidence_hash(evidence_payload),
    )


def _render_text(
    *,
    trade_date: str,
    event_id: str,
    data_origin: str,
    status: str,
    fact_status: FactStatus,
    metrics: Mapping[str, Any],
    changes: Mapping[str, str],
    reason_codes: Tuple[str, ...],
    market_summary: Optional[AuctionMarketSummaryFact],
) -> str:
    """Render only objective facts; strategy language is intentionally absent."""

    lines = [
        f"# 竞价事实观察 {trade_date} {event_id}",
        f"数据状态：{status}；事实状态：{fact_status.value}；来源：{data_origin}",
        "",
        "## Metrics",
    ]
    for key in sorted(metrics):
        lines.append(f"- {key}: {_display(metrics[key])}")
    if market_summary is not None:
        lines.extend([
            "",
            "## A2 Market Summary",
            f"- status: {market_summary.status.value}",
            f"- stock_count: {_display(market_summary.stock_count)}",
            f"- valid_stock_count: {_display(market_summary.valid_stock_count)}",
            f"- unavailable_stock_count: {_display(market_summary.unavailable_stock_count)}",
            f"- positive_count: {_display(market_summary.positive_count)}",
            f"- negative_count: {_display(market_summary.negative_count)}",
            f"- flat_count: {_display(market_summary.flat_count)}",
            f"- auction_amount_yuan: {_display(market_summary.auction_amount_yuan)}",
            f"- limit_up_count: {_display(market_summary.limit_up_count)}",
            f"- limit_down_count: {_display(market_summary.limit_down_count)}",
            f"- limit_up_seal_amount_yuan: {_display(market_summary.limit_up_seal_amount_yuan)}",
        ])
    lines.extend(["", "## Changes"])
    for key in sorted(changes):
        lines.append(f"- {key}: {_display(changes[key])}")
    lines.extend(["", "## Reason codes"])
    if reason_codes:
        lines.extend(f"- {_display(item)}" for item in reason_codes)
    else:
        lines.append("- unavailable")
    return "\n".join(lines) + "\n"
