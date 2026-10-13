"""Rebuild the auction facts a late-started session missed, from TD rows.

Pure over already-read ``auction_snapshot_v2`` rows (see
``examples/run_real_td_auction_rows.py``).  It stitches the existing pure
adapters: per-tag summaries (``derive_td_auction_summary``), the 0925 current
rows used by the previous-day feedback, and the 0924 -> 0925 theme delta rows.
Each part reports its own status; a tag with no rows is UNAVAILABLE, never an
all-zero summary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Tuple

from .contracts import canonical_hash
from .facts import FactStatus
from .previous_day_limit_current_rows import Current0925Rows, build_current_0925_rows
from .td_auction_summary import derive_td_auction_summary
from .theme_auction_delta_rows import ThemeDeltaRows, build_theme_delta_rows

TD_RECONSTRUCTION_CONTRACT_VERSION = "TdAuctionReconstructionV1"
TAGS = ("0920", "0924", "0925")


@dataclass(frozen=True)
class TdAuctionReconstruction:
    trade_date: str
    status: FactStatus
    summaries: Mapping[str, Mapping[str, Any]]
    tag_status: Mapping[str, str]
    current_0925: Current0925Rows
    theme_delta: ThemeDeltaRows
    row_counts: Mapping[str, int]
    excluded_other_date_rows: int = 0
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "content_hash", canonical_hash({
            "contract": TD_RECONSTRUCTION_CONTRACT_VERSION,
            "trade_date": self.trade_date,
            "excluded_other_date_rows": self.excluded_other_date_rows,
            "summaries": {k: dict(v) for k, v in self.summaries.items()},
            "current_0925": self.current_0925.content_hash,
            "theme_delta": self.theme_delta.content_hash,
        }))

    def as_trace(self) -> Mapping[str, Any]:
        return {
            "contract": TD_RECONSTRUCTION_CONTRACT_VERSION,
            "trade_date": self.trade_date,
            "status": self.status,
            "tag_status": dict(self.tag_status),
            "row_counts": dict(self.row_counts),
            "excluded_other_date_rows": self.excluded_other_date_rows,
            "summaries": {k: dict(v) for k, v in self.summaries.items()},
            "current_0925": self.current_0925.as_trace(),
            "theme_delta": self.theme_delta.as_trace(),
            "content_hash": self.content_hash,
        }


def reconstruct_auction_from_td(
    trade_date: str, td_rows: Iterable[Mapping[str, Any]]
) -> TdAuctionReconstruction:
    rows = []
    excluded_other_date_rows = 0
    # TD stores either compact dates or YYYY-MM-DD; adapters use YYYY-MM-DD.
    compact = trade_date.replace("-", "")
    for source_row in td_rows:
        row = dict(source_row)
        if str(row.get("trade_date", "")).replace("-", "") != compact:
            excluded_other_date_rows += 1
            continue
        row["trade_date"] = trade_date
        rows.append(row)
    counts = {tag: sum(1 for r in rows if str(r.get("auction_tag")) == tag) for tag in TAGS}
    summaries = {}
    tag_status = {}
    for tag in TAGS:
        if counts[tag] == 0:
            tag_status[tag] = FactStatus.UNAVAILABLE.value
            continue
        summaries[tag] = derive_td_auction_summary(rows, tag=tag)
        partial = summaries[tag]["unknown_amount_rows"] or summaries[tag]["duplicate_symbol_rows"]
        tag_status[tag] = FactStatus.PARTIAL.value if partial else FactStatus.READY.value
    current = build_current_0925_rows(trade_date, rows)
    delta = build_theme_delta_rows(trade_date, rows)
    if not summaries:
        status = FactStatus.UNAVAILABLE
    elif excluded_other_date_rows or any(v != FactStatus.READY.value for v in tag_status.values()):
        status = FactStatus.PARTIAL
    else:
        status = FactStatus.READY
    return TdAuctionReconstruction(
        trade_date=trade_date, status=status, summaries=summaries, tag_status=tag_status,
        current_0925=current, theme_delta=delta, row_counts=counts,
        excluded_other_date_rows=excluded_other_date_rows,
    )


__all__ = ["TD_RECONSTRUCTION_CONTRACT_VERSION", "TdAuctionReconstruction", "reconstruct_auction_from_td"]
