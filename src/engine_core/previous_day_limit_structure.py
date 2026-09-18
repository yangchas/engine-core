"""Pure structural facts derived from the previous-session limit-up pool.

This module deliberately stops before current-session return feedback.  The
pool provider supplies the previous-session membership and board height; the
09:25/09:30 observation needed for return feedback belongs to a different
fact input and must not be invented here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Mapping, Optional, Tuple

from .contracts import DataResult, DataStatus, deep_freeze, evidence_hash, semantic_hash
from .facts import FactStatus


PREVIOUS_DAY_LIMIT_STRUCTURE_CONTRACT_VERSION = "PreviousDayLimitStructureFactV1"
_SYMBOL_PATTERN = re.compile(r"^\d{6}$")


@dataclass(frozen=True)
class PreviousDayLimitStructureFact:
    """Objective previous-session structure, without strategy conclusions."""

    previous_trade_date: Optional[str]
    status: FactStatus
    source_id: Optional[str]
    row_count: Optional[int]
    highest_board_height: Optional[int]
    highest_board_symbols: Tuple[str, ...]
    board_height_distribution: Mapping[str, int]
    missing_fields: Tuple[str, ...] = ()
    invalid_fields: Tuple[str, ...] = ()
    source_content_hash: Optional[str] = None
    source_evidence_hash: Optional[str] = None
    evidence_refs: Tuple[str, ...] = ()
    content_hash: str = field(init=False)
    evidence_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.row_count is not None and (
            isinstance(self.row_count, bool)
            or not isinstance(self.row_count, int)
            or self.row_count < 0
        ):
            raise ValueError("row_count must be a non-negative integer or None")
        if self.highest_board_height is not None and (
            isinstance(self.highest_board_height, bool)
            or not isinstance(self.highest_board_height, int)
            or self.highest_board_height < 1
        ):
            raise ValueError("highest_board_height must be a positive integer or None")
        symbols = tuple(self.highest_board_symbols)
        if any(not isinstance(symbol, str) or _SYMBOL_PATTERN.fullmatch(symbol) is None for symbol in symbols):
            raise ValueError("highest_board_symbols must contain six-digit symbols")
        if symbols != tuple(sorted(set(symbols))):
            raise ValueError("highest_board_symbols must be unique and sorted")
        distribution = {str(key): int(value) for key, value in self.board_height_distribution.items()}
        if any(value < 0 for value in distribution.values()):
            raise ValueError("board height counts must be non-negative")
        object.__setattr__(self, "highest_board_symbols", symbols)
        object.__setattr__(
            self,
            "board_height_distribution",
            deep_freeze(dict(sorted(distribution.items()))),
        )
        object.__setattr__(self, "missing_fields", tuple(sorted(set(self.missing_fields))))
        object.__setattr__(self, "invalid_fields", tuple(sorted(set(self.invalid_fields))))
        object.__setattr__(self, "evidence_refs", tuple(sorted(set(self.evidence_refs))))

        semantic_payload = {
            "contract_version": PREVIOUS_DAY_LIMIT_STRUCTURE_CONTRACT_VERSION,
            "previous_trade_date": self.previous_trade_date,
            "status": self.status,
            "row_count": self.row_count,
            "highest_board_height": self.highest_board_height,
            "highest_board_symbols": self.highest_board_symbols,
            "board_height_distribution": self.board_height_distribution,
            "missing_fields": self.missing_fields,
            "invalid_fields": self.invalid_fields,
        }
        evidence_payload = {
            "contract_version": PREVIOUS_DAY_LIMIT_STRUCTURE_CONTRACT_VERSION,
            "previous_trade_date": self.previous_trade_date,
            "source_id": self.source_id,
            "source_content_hash": self.source_content_hash,
            "source_evidence_hash": self.source_evidence_hash,
            "evidence_refs": self.evidence_refs,
        }
        object.__setattr__(self, "content_hash", semantic_hash(semantic_payload))
        object.__setattr__(self, "evidence_hash", evidence_hash(evidence_payload))

    def as_mapping(self) -> Mapping[str, Any]:
        return {
            "contract_version": PREVIOUS_DAY_LIMIT_STRUCTURE_CONTRACT_VERSION,
            "previous_trade_date": self.previous_trade_date,
            "status": self.status,
            "source_id": self.source_id,
            "row_count": self.row_count,
            "highest_board_height": self.highest_board_height,
            "highest_board_symbols": self.highest_board_symbols,
            "board_height_distribution": self.board_height_distribution,
            "missing_fields": self.missing_fields,
            "invalid_fields": self.invalid_fields,
            "source_content_hash": self.source_content_hash,
            "source_evidence_hash": self.source_evidence_hash,
            "evidence_refs": self.evidence_refs,
            "content_hash": self.content_hash,
            "evidence_hash": self.evidence_hash,
        }


def build_previous_day_limit_structure(
    result: DataResult,
) -> PreviousDayLimitStructureFact:
    """Build structural facts from one guarded previous-day pool result.

    ``UNAVAILABLE`` data never leaks its rows into the fact.  A caller may
    still inspect the source ``DataResult`` for evidence, but runtime facts
    remain fail-closed until the data function has passed its temporal gate.
    """

    if not isinstance(result, DataResult):
        raise TypeError("result must be a DataResult")
    refs = tuple(
        provenance.evidence_ref
        for provenance in result.provenance
        if provenance.evidence_ref
    )
    base = {
        "previous_trade_date": result.actual_trade_date,
        "source_id": result.actual_source,
        "source_content_hash": result.content_hash,
        "source_evidence_hash": evidence_hash(
            {
                "provenance": tuple(
                    (item.source_id, item.source_schema, item.source_trade_date, item.evidence_ref)
                    for item in result.provenance
                )
            }
        ),
        "evidence_refs": refs,
    }
    if result.status not in {DataStatus.READY, DataStatus.PARTIAL}:
        status = (
            FactStatus.MISSING
            if result.status is DataStatus.MISSING
            else FactStatus.INVALID
            if result.status is DataStatus.INVALID
            else FactStatus.UNAVAILABLE
        )
        return PreviousDayLimitStructureFact(
            status=status,
            row_count=None,
            highest_board_height=None,
            highest_board_symbols=(),
            board_height_distribution={},
            missing_fields=tuple(result.missing_fields),
            **base,
        )

    data = result.data
    rows = data.get("rows") if isinstance(data, Mapping) else None
    if not isinstance(rows, (list, tuple)):
        return PreviousDayLimitStructureFact(
            status=FactStatus.INVALID,
            row_count=None,
            highest_board_height=None,
            highest_board_symbols=(),
            board_height_distribution={},
            invalid_fields=("rows",),
            **base,
        )

    heights: list[tuple[str, int]] = []
    invalid_fields: list[str] = []
    seen: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            invalid_fields.append(f"rows[{index}]")
            continue
        symbol = row.get("symbol")
        height = row.get("lb_days")
        if not isinstance(symbol, str) or _SYMBOL_PATTERN.fullmatch(symbol) is None:
            invalid_fields.append(f"rows[{index}].symbol")
            continue
        if symbol in seen:
            invalid_fields.append(f"rows[{index}].symbol_duplicate")
            continue
        seen.add(symbol)
        if isinstance(height, bool) or not isinstance(height, int) or height < 1:
            invalid_fields.append(f"rows[{index}].lb_days")
            continue
        heights.append((symbol, height))

    if invalid_fields:
        return PreviousDayLimitStructureFact(
            status=FactStatus.INVALID,
            row_count=len(rows),
            highest_board_height=None,
            highest_board_symbols=(),
            board_height_distribution={},
            invalid_fields=tuple(invalid_fields),
            **base,
        )
    distribution: dict[str, int] = {}
    for _, height in heights:
        key = str(height)
        distribution[key] = distribution.get(key, 0) + 1
    highest = max((height for _, height in heights), default=None)
    highest_symbols = tuple(sorted(symbol for symbol, height in heights if height == highest))
    return PreviousDayLimitStructureFact(
        status=FactStatus.PARTIAL if result.status is DataStatus.PARTIAL else FactStatus.READY,
        row_count=len(heights),
        highest_board_height=highest,
        highest_board_symbols=highest_symbols,
        board_height_distribution=distribution,
        missing_fields=tuple(result.missing_fields),
        **base,
    )


__all__ = [
    "PREVIOUS_DAY_LIMIT_STRUCTURE_CONTRACT_VERSION",
    "PreviousDayLimitStructureFact",
    "build_previous_day_limit_structure",
]
