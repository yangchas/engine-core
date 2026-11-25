"""Offline TD schema proposal and guarded optional writer interface."""
from __future__ import annotations

import re

ALLOWED_DATABASES = {"core_data", "core_intraday"}


def validate_database_name(name: str) -> str:
    if name not in ALLOWED_DATABASES or not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        raise ValueError("td_database_not_allowlisted")
    return name


DDL = (
    "CREATE DATABASE IF NOT EXISTS core_data KEEP 730 DURATION 30 BUFFER 64",
    "CREATE STABLE IF NOT EXISTS core_data.daily_bar_v1 (ts TIMESTAMP,trade_date VARCHAR(10),open_scaled INT,high_scaled INT,low_scaled INT,close_scaled INT,pre_close_scaled INT,amount_yuan BIGINT,volume_shares BIGINT,pct_bp INT,source VARCHAR(32),available_at TIMESTAMP,revision VARCHAR(32),quality VARCHAR(64)) TAGS (symbol NCHAR(6),source_id NCHAR(32))",
    "CREATE DATABASE IF NOT EXISTS core_intraday KEEP 30 DURATION 1",
)


class TDWriter:
    """Adapter delegates to an explicitly supplied client; construction never connects."""
    def __init__(self, client: object, database: str):
        self.client = client
        self.database = validate_database_name(database)

    def write_dataset(self, spec: object, trade_date: str, rows: list[dict], manifest: dict) -> None:
        if self.database != "core_data":
            raise ValueError("td_database_not_allowlisted")
        writer = getattr(self.client, "upsert_rows", None)
        if writer is None:
            raise RuntimeError("td_upsert_protocol_missing")
        td_rows = []
        for row in rows:
            projected = dict(row)
            if projected.get("ts") in (None, "") and getattr(spec, "name", None) == "daily_kline":
                projected["ts"] = f"{trade_date}T00:00:00"
            if projected.get("symbol") in (None, "") or projected.get("ts") in (None, ""):
                raise ValueError("td_primary_key_missing:ts+symbol")
            td_rows.append(projected)
        expected = len(td_rows)
        writer(self.database, spec, trade_date, td_rows, manifest, primary_key=("ts", "symbol"))
        counter = getattr(self.client, "count_rows", None)
        if counter is None:
            raise RuntimeError("td_count_protocol_missing")
        actual = counter(self.database, spec, trade_date)
        if actual != expected:
            raise RuntimeError("td_row_count_mismatch")


def apply_ddl(client: object, database: str) -> None:
    selected = validate_database_name(database)
    if selected != "core_data":
        raise ValueError("td_database_not_allowlisted")
    execute = getattr(client, "execute", None)
    if execute is None:
        raise RuntimeError("td_execute_protocol_missing")
    for statement in DDL:
        if not statement.startswith("CREATE DATABASE IF NOT EXISTS core_data") and "core_data." not in statement:
            continue
        if "market_data1" in statement.lower():
            raise ValueError("td_database_not_allowlisted")
        execute(statement)
