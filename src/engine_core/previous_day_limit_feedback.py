"""Current-session feedback for a previous-session limit-up pool.

This is the smallest fact-only extraction of the deployed
``_build_prior_limit_up_structure`` path.  It joins an already guarded
previous-session pool with an already normalized current ``0925`` anchor.
The function does not fetch data, infer a source timestamp, calculate a
strategy score, or aggregate plates.  ``change_pct`` is an explicit
percentage-point field supplied by the current-anchor adapter; it is never
derived from an unrelated price field.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import math
import re
from typing import Any, Iterable, Mapping, Optional, Tuple

from .contracts import DataResult, DataStatus, deep_freeze, evidence_hash, semantic_hash
from .facts import FactStatus


PREVIOUS_DAY_LIMIT_FEEDBACK_CONTRACT_VERSION = "PreviousDayLimitFeedbackFactV1"
_SYMBOL_PATTERN = re.compile(r"^\d{6}$")
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _strict_date(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or _DATE_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field} must be strict YYYY-MM-DD")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be a valid YYYY-MM-DD")
    return value


def _finite_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _source_time(row: Mapping[str, Any]) -> Optional[int]:
    value = row.get("source_record_time_ms")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _median(values: list[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


@dataclass(frozen=True)
class PreviousDayLimitFeedbackFact:
    """Objective 0925 feedback for members of a prior limit-up pool.

    ``current_change_pct`` uses percentage-point units (``5.0`` means five
    percent), matching the audited current-anchor consumer contract.  A
    missing value remains missing; it is never treated as zero.  The fact is
    descriptive only and does not contain a strategy conclusion.
    """

    previous_trade_date: Optional[str]
    current_trade_date: Optional[str]
    business_anchor: str
    status: FactStatus
    prior_limit_up_count: Optional[int]
    valid_return_count: Optional[int]
    return_unavailable_count: Optional[int]
    up_count: Optional[int]
    down_count: Optional[int]
    flat_count: Optional[int]
    up_ratio: Optional[float]
    median_return_pct: Optional[float]
    highest_board_height: Optional[int]
    highest_board_symbols: Tuple[str, ...]
    board_height_distribution: Mapping[str, int]
    records: Tuple[Mapping[str, Any], ...] = ()
    missing_fields: Tuple[str, ...] = ()
    invalid_fields: Tuple[str, ...] = ()
    source_time_min_ms: Optional[int] = None
    source_time_max_ms: Optional[int] = None
    source_content_hash: Optional[str] = None
    source_evidence_hash: Optional[str] = None
    evidence_refs: Tuple[str, ...] = ()
    content_hash: str = field(init=False)
    evidence_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.business_anchor != "0925":
            raise ValueError("business_anchor must be 0925")
        if self.prior_limit_up_count is not None and self.prior_limit_up_count < 0:
            raise ValueError("prior_limit_up_count must be non-negative")
        for name in ("valid_return_count", "return_unavailable_count", "up_count", "down_count", "flat_count"):
            value = getattr(self, name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise ValueError(f"{name} must be a non-negative integer or None")
        if self.up_ratio is not None and not 0.0 <= self.up_ratio <= 1.0:
            raise ValueError("up_ratio must be between 0 and 1")
        if self.source_time_min_ms is not None and self.source_time_min_ms <= 0:
            raise ValueError("source_time_min_ms must be positive")
        if self.source_time_max_ms is not None and self.source_time_max_ms <= 0:
            raise ValueError("source_time_max_ms must be positive")
        if (
            self.source_time_min_ms is not None
            and self.source_time_max_ms is not None
            and self.source_time_min_ms > self.source_time_max_ms
        ):
            raise ValueError("source time range is inverted")
        if any(
            not isinstance(symbol, str) or _SYMBOL_PATTERN.fullmatch(symbol) is None
            for symbol in self.highest_board_symbols
        ):
            raise ValueError("highest_board_symbols must contain six-digit symbols")
        object.__setattr__(self, "records", tuple(deep_freeze(item) for item in self.records))
        object.__setattr__(self, "highest_board_symbols", tuple(sorted(set(self.highest_board_symbols))))
        object.__setattr__(self, "board_height_distribution", deep_freeze(dict(sorted(self.board_height_distribution.items()))))
        object.__setattr__(self, "missing_fields", tuple(sorted(set(self.missing_fields))))
        object.__setattr__(self, "invalid_fields", tuple(sorted(set(self.invalid_fields))))
        object.__setattr__(self, "evidence_refs", tuple(sorted(set(self.evidence_refs))))
        semantic_payload = {
            "contract_version": PREVIOUS_DAY_LIMIT_FEEDBACK_CONTRACT_VERSION,
            "previous_trade_date": self.previous_trade_date,
            "current_trade_date": self.current_trade_date,
            "business_anchor": self.business_anchor,
            "status": self.status,
            "prior_limit_up_count": self.prior_limit_up_count,
            "valid_return_count": self.valid_return_count,
            "return_unavailable_count": self.return_unavailable_count,
            "up_count": self.up_count,
            "down_count": self.down_count,
            "flat_count": self.flat_count,
            "up_ratio": self.up_ratio,
            "median_return_pct": self.median_return_pct,
            "highest_board_height": self.highest_board_height,
            "highest_board_symbols": self.highest_board_symbols,
            "board_height_distribution": self.board_height_distribution,
            "records": self.records,
            "missing_fields": self.missing_fields,
            "invalid_fields": self.invalid_fields,
            "source_time_min_ms": self.source_time_min_ms,
            "source_time_max_ms": self.source_time_max_ms,
        }
        evidence_payload = {
            "contract_version": PREVIOUS_DAY_LIMIT_FEEDBACK_CONTRACT_VERSION,
            "previous_trade_date": self.previous_trade_date,
            "current_trade_date": self.current_trade_date,
            "business_anchor": self.business_anchor,
            "source_time_min_ms": self.source_time_min_ms,
            "source_time_max_ms": self.source_time_max_ms,
            "source_content_hash": self.source_content_hash,
            "source_evidence_hash": self.source_evidence_hash,
            "evidence_refs": self.evidence_refs,
        }
        object.__setattr__(self, "content_hash", semantic_hash(semantic_payload))
        object.__setattr__(self, "evidence_hash", evidence_hash(evidence_payload))

    def as_mapping(self) -> Mapping[str, Any]:
        return {
            "contract_version": PREVIOUS_DAY_LIMIT_FEEDBACK_CONTRACT_VERSION,
            "previous_trade_date": self.previous_trade_date,
            "current_trade_date": self.current_trade_date,
            "business_anchor": self.business_anchor,
            "status": self.status,
            "prior_limit_up_count": self.prior_limit_up_count,
            "valid_return_count": self.valid_return_count,
            "return_unavailable_count": self.return_unavailable_count,
            "up_count": self.up_count,
            "down_count": self.down_count,
            "flat_count": self.flat_count,
            "up_ratio": self.up_ratio,
            "median_return_pct": self.median_return_pct,
            "highest_board_height": self.highest_board_height,
            "highest_board_symbols": self.highest_board_symbols,
            "board_height_distribution": self.board_height_distribution,
            "records": self.records,
            "missing_fields": self.missing_fields,
            "invalid_fields": self.invalid_fields,
            "source_time_min_ms": self.source_time_min_ms,
            "source_time_max_ms": self.source_time_max_ms,
            "source_content_hash": self.source_content_hash,
            "source_evidence_hash": self.source_evidence_hash,
            "evidence_refs": self.evidence_refs,
            "content_hash": self.content_hash,
            "evidence_hash": self.evidence_hash,
        }


def _unavailable_feedback(
    previous_result: DataResult,
    *,
    current_trade_date: str,
    missing_fields: Tuple[str, ...] = (),
    invalid_fields: Tuple[str, ...] = (),
) -> PreviousDayLimitFeedbackFact:
    refs = tuple(item.evidence_ref for item in previous_result.provenance if item.evidence_ref)
    return PreviousDayLimitFeedbackFact(
        previous_trade_date=previous_result.actual_trade_date,
        current_trade_date=current_trade_date,
        business_anchor="0925",
        status=FactStatus.UNAVAILABLE,
        prior_limit_up_count=None,
        valid_return_count=None,
        return_unavailable_count=None,
        up_count=None,
        down_count=None,
        flat_count=None,
        up_ratio=None,
        median_return_pct=None,
        highest_board_height=None,
        highest_board_symbols=(),
        board_height_distribution={},
        missing_fields=missing_fields,
        invalid_fields=invalid_fields,
        source_content_hash=previous_result.content_hash,
        source_evidence_hash=evidence_hash({"provenance": tuple((item.source_id, item.evidence_ref) for item in previous_result.provenance)}),
        evidence_refs=refs,
    )


def build_previous_day_limit_feedback(
    previous_result: DataResult,
    current_rows: Iterable[Mapping[str, Any]],
    *,
    current_trade_date: str,
) -> PreviousDayLimitFeedbackFact:
    """Join a guarded previous pool with normalized 0925 current rows.

    ``current_rows`` must already be adapted to this contract.  Each row must
    contain ``symbol``, ``trade_date``, ``tag='0925'`` and a percentage-point
    ``change_pct`` when available.  ``source_record_time_ms`` is evidence of
    the source row time and is never replaced with the business anchor time.
    """

    if not isinstance(previous_result, DataResult):
        raise TypeError("previous_result must be a DataResult")
    current_trade_date = _strict_date(current_trade_date, field="current_trade_date")
    if previous_result.status not in {DataStatus.READY, DataStatus.PARTIAL}:
        return _unavailable_feedback(previous_result, current_trade_date=current_trade_date)
    data = previous_result.data
    rows = data.get("rows") if isinstance(data, Mapping) else None
    if not isinstance(rows, (list, tuple)):
        return _unavailable_feedback(previous_result, current_trade_date=current_trade_date, invalid_fields=("previous.rows",))

    current_by_symbol: dict[str, Mapping[str, Any]] = {}
    invalid: list[str] = []
    for index, row in enumerate(current_rows):
        if not isinstance(row, Mapping):
            invalid.append(f"current[{index}]")
            continue
        symbol = row.get("symbol")
        if not isinstance(symbol, str) or _SYMBOL_PATTERN.fullmatch(symbol) is None:
            invalid.append(f"current[{index}].symbol")
            continue
        if row.get("trade_date") != current_trade_date:
            invalid.append(f"current[{index}].trade_date")
            continue
        if row.get("tag") != "0925":
            invalid.append(f"current[{index}].tag")
            continue
        if symbol in current_by_symbol:
            invalid.append(f"current[{index}].symbol_duplicate")
            continue
        current_by_symbol[symbol] = row

    if invalid:
        return _unavailable_feedback(
            previous_result,
            current_trade_date=current_trade_date,
            invalid_fields=tuple(invalid),
        )

    records: list[dict[str, Any]] = []
    valid_returns: list[float] = []
    missing_returns = 0
    source_times: list[int] = []
    heights: list[tuple[str, int]] = []
    missing_fields: set[str] = set(previous_result.missing_fields)
    for index, previous in enumerate(rows):
        if not isinstance(previous, Mapping):
            return _unavailable_feedback(
                previous_result,
                current_trade_date=current_trade_date,
                invalid_fields=(f"previous.rows[{index}]",),
            )
        symbol = previous.get("symbol")
        height = previous.get("lb_days")
        if not isinstance(symbol, str) or _SYMBOL_PATTERN.fullmatch(symbol) is None:
            return _unavailable_feedback(
                previous_result,
                current_trade_date=current_trade_date,
                invalid_fields=(f"previous.rows[{index}].symbol",),
            )
        if isinstance(height, bool) or not isinstance(height, int) or height < 1:
            return _unavailable_feedback(
                previous_result,
                current_trade_date=current_trade_date,
                invalid_fields=(f"previous.rows[{index}].lb_days",),
            )
        current = current_by_symbol.get(symbol, {})
        change = _finite_number(current.get("change_pct"))
        source_time = _source_time(current)
        if current and source_time is not None:
            source_times.append(source_time)
        if change is None:
            missing_returns += 1
            missing_fields.add(f"change_pct:{symbol}")
        else:
            valid_returns.append(change)
        if current and source_time is None:
            missing_fields.add(f"source_record_time_ms:{symbol}")
        heights.append((symbol, height))
        records.append({
            "symbol": symbol,
            "name": previous.get("name"),
            "lb_days": height,
            "plate": previous.get("plate"),
            "current_change_pct": change,
            "auction_amount_yuan": current.get("auction_amount_yuan") if current else None,
            "source_record_time_ms": source_time,
        })

    row_count = len(records)
    highest = max((height for _, height in heights), default=None)
    distribution: dict[str, int] = {}
    for _, height in heights:
        distribution[str(height)] = distribution.get(str(height), 0) + 1
    up = sum(value > 0 for value in valid_returns)
    down = sum(value < 0 for value in valid_returns)
    flat = sum(value == 0 for value in valid_returns)
    status = (
        FactStatus.READY
        if previous_result.status is DataStatus.READY
        and valid_returns
        and not missing_returns
        and not any(field.startswith("source_record_time_ms:") for field in missing_fields)
        else FactStatus.PARTIAL
    )
    refs = tuple(item.evidence_ref for item in previous_result.provenance if item.evidence_ref)
    return PreviousDayLimitFeedbackFact(
        previous_trade_date=previous_result.actual_trade_date,
        current_trade_date=current_trade_date,
        business_anchor="0925",
        status=status,
        prior_limit_up_count=row_count,
        valid_return_count=len(valid_returns),
        return_unavailable_count=missing_returns,
        up_count=up,
        down_count=down,
        flat_count=flat,
        up_ratio=(up / len(valid_returns)) if valid_returns else None,
        median_return_pct=_median(valid_returns),
        highest_board_height=highest,
        highest_board_symbols=tuple(sorted(symbol for symbol, height in heights if height == highest)),
        board_height_distribution=distribution,
        records=tuple(records),
        missing_fields=tuple(sorted(missing_fields)),
        source_time_min_ms=min(source_times) if source_times else None,
        source_time_max_ms=max(source_times) if source_times else None,
        source_content_hash=previous_result.content_hash,
        source_evidence_hash=evidence_hash({"provenance": tuple((item.source_id, item.evidence_ref) for item in previous_result.provenance)}),
        evidence_refs=refs,
    )


__all__ = [
    "PREVIOUS_DAY_LIMIT_FEEDBACK_CONTRACT_VERSION",
    "PreviousDayLimitFeedbackFact",
    "build_previous_day_limit_feedback",
]
