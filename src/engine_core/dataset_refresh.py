"""Read-time schedule and source freshness summaries for legacy datasets."""

from __future__ import annotations

from typing import Any, Mapping

from .live_inputs import LiveInputState


DATASET_SCHEDULE = (
    {
        "name": "hot_plates",
        "legacy_writer_phases": ("PREMARKET", "AUCTION", "INTRADAY", "POSTMARKET"),
        "read_at": ("09:15:30", "09:26:05", "09:32:30"),
        "description": "Legacy hot plate cache, read at scheduled node checkpoints.",
    },
    {
        "name": "hot_rank",
        "legacy_writer_phases": ("PREMARKET", "AUCTION", "INTRADAY", "POSTMARKET"),
        "read_at": ("09:15:30", "09:26:05", "09:32:30"),
        "description": "Legacy hot rank cache, read at scheduled node checkpoints.",
    },
    {
        "name": "limit_truth",
        "legacy_writer_phases": ("POSTMARKET",),
        "read_at": ("15:10:00",),
        "description": "Legacy limit truth cache, read after market close.",
    },
    {
        "name": "yest_limit_pool",
        "legacy_writer_phases": ("PREMARKET", "AUCTION", "INTRADAY", "POSTMARKET"),
        "read_at": ("09:15:30", "09:26:05", "09:32:30"),
        "description": "Previous trading day's limit pool cache.",
    },
    {
        "name": "stock_plate",
        "legacy_writer_phases": ("PREMARKET", "AUCTION", "INTRADAY", "POSTMARKET"),
        "read_at": ("09:15:30", "09:26:05", "09:32:30"),
        "description": "Legacy stock to plate mapping.",
    },
    {
        "name": "s2p",
        "legacy_writer_phases": None,
        "read_at": ("09:15:30", "09:26:05", "09:32:30"),
        "description": "Legacy symbol to plate mapping.",
    },
    {
        "name": "anchor_archive",
        "legacy_writer_phases": ("AUCTION", "INTRADAY"),
        "read_at": ("09:26:05", "09:32:30"),
        "description": "Legacy auction anchor archive.",
    },
)


def _updated_at_ts(meta: Any) -> int | None:
    if not isinstance(meta, Mapping):
        return None
    value = meta.get("updated_at_ts")
    if isinstance(value, bool):
        return None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def summarize_dataset_state(
    state: LiveInputState, now_ms: int, meta: Any = None,
) -> dict[str, Any]:
    """Summarize only values directly present in a read/state/metadata object."""
    read = state.read
    source_meta = meta if meta is not None else (read.meta if read is not None else None)
    updated_at_ts = _updated_at_ts(source_meta)
    if read is None:
        row_count = None
        key = None
    elif read.kind == "hash":
        row_count = len(read.rows)
        key = read.key
    else:
        payload = read.payload
        row_count = len(payload) if isinstance(payload, (dict, list)) else None
        key = read.key
    return {
        "name": state.name,
        "status": state.status,
        "row_count": row_count,
        "key": key,
        "last_refreshed_ms": state.last_refreshed_ms,
        "meta_phase": source_meta.get("phase") if isinstance(source_meta, Mapping) else None,
        "meta_updated_at": source_meta.get("updated_at") if isinstance(source_meta, Mapping) else None,
        "meta_updated_at_ts": updated_at_ts,
        "writer_age_ms": now_ms - updated_at_ts * 1000 if updated_at_ts is not None else None,
        "reason": state.reason,
    }


__all__ = ["DATASET_SCHEDULE", "summarize_dataset_state"]
