"""Offline TD schema proposal and guarded optional writer interface."""
from __future__ import annotations

import re

ALLOWED_DATABASES = {"core_data", "core_intraday"}


def validate_database_name(name: str) -> str:
    if name not in ALLOWED_DATABASES or not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        raise ValueError("td_database_not_allowlisted")
    return name


DDL = (
    "CREATE DATABASE IF NOT EXISTS core_data KEEP 730 DURATION 30",
    "CREATE STABLE IF NOT EXISTS core_data.daily_bar_v1 (ts TIMESTAMP,trade_date VARCHAR(10),open_scaled INT,high_scaled INT,low_scaled INT,close_scaled INT,pre_close_scaled INT,amount_yuan BIGINT,volume_shares BIGINT,pct_bp INT,source VARCHAR(32),available_at TIMESTAMP,revision VARCHAR(32),quality VARCHAR(64)) TAGS (symbol NCHAR(6),source_id NCHAR(32))",
    "CREATE DATABASE IF NOT EXISTS core_intraday KEEP 30 DURATION 1",
)


class TDWriter:
    """Adapter delegates to an explicitly supplied client; construction never connects."""
    def __init__(self, client: object, database: str):
        self.client = client
        self.database = validate_database_name(database)

    def write_dataset(self, spec: object, trade_date: str, rows: list[dict], manifest: dict) -> None:
        writer = getattr(self.client, "write_dataset", None)
        if writer is None: raise RuntimeError("td_writer_protocol_missing")
        writer(self.database, spec, trade_date, rows, manifest)
