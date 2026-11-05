"""Append-only TD snapshots with a single ACTIVE version marker for reference data."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping

from .td_sink import td_object_missing


_TABLES = {
    "plate_members": "plate_members_latest",
    "plate_registry": "plate_registry_latest",
    "stock_static": "stock_static_latest",
}
_META_TABLE = "reference_snapshot_versions"
_DB_RE = re.compile(r"^(?:core_data|core_dev_[a-z0-9_]{1,24})$")


class ReferenceSnapshotError(RuntimeError):
    """A complete reference snapshot could not be staged and activated."""


def payload_json(row: Mapping[str, Any]) -> str:
    return json.dumps(dict(row), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def rows_sha256(payloads: Iterable[str]) -> str:
    return hashlib.sha256("\n".join(payloads).encode("utf-8")).hexdigest()


def _literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _stamp(value: datetime | None = None) -> str:
    stamp = value or datetime.now(timezone.utc)
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(timezone.utc).replace(tzinfo=None)
    return stamp.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


class ReferenceSnapshotStore:
    """Store full reference snapshots, exposing only the last ACTIVE version.

    TD does not provide a transaction spanning a large row set. New rows and
    STAGING markers are append-only; one ACTIVE marker is appended after the
    full row-count/hash readback succeeds. Readers filter by that marker's
    version, so an interrupted refresh leaves the previous version visible.
    """

    def __init__(self, database: str | None = None, *, connect: Callable[[], Any] | None = None,
                 batch_size: int = 500):
        self.database = database or os.environ.get("CORE_TD_DATABASE") or "core_data"
        if not _DB_RE.fullmatch(self.database):
            raise ValueError("reference_snapshot_database_not_allowed")
        self._connect_fn = connect or self._default_connect
        self.batch_size = max(1, int(batch_size))
        self._conn: Any = None

    def _default_connect(self):
        config = os.environ.get("TD_CONFIG")
        if not config:
            raise ReferenceSnapshotError("TD_CONFIG_required")
        from .td_sink import connect_td
        return connect_td(self.database)

    def _cursor(self):
        if self._conn is None:
            self._conn = self._connect_fn()
        return self._conn.cursor()

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def ensure_schema(self) -> None:
        cursor = self._cursor()
        cursor.execute(
            f"CREATE TABLE IF NOT EXISTS {self.database}.{_META_TABLE} ("
            "ts TIMESTAMP,dataset VARCHAR(32),version VARCHAR(40),snapshot_state VARCHAR(16),"
            "row_count INT,sha256 VARCHAR(64),source VARCHAR(96),available_at VARCHAR(40),"
            "request_count INT,elapsed_ms BIGINT)"
        )
        for table in _TABLES.values():
            cursor.execute(
                f"CREATE TABLE IF NOT EXISTS {self.database}.{table} ("
                "ts TIMESTAMP,version VARCHAR(40),row_id VARCHAR(64),plate_code VARCHAR(16),"
                "symbol VARCHAR(16),source VARCHAR(96),available_at VARCHAR(40),payload NCHAR(4096))"
            )

    def _next_epoch_ms(self, table: str) -> int:
        cursor = self._cursor()
        cursor.execute(f"SELECT ts FROM {self.database}.{table} ORDER BY ts DESC LIMIT 1")
        row = cursor.fetchone()
        now = int(time.time() * 1000)
        if row and row[0] is not None:
            prior = row[0]
            try:
                prior_ms = int(prior.timestamp() * 1000)
                now = max(now, prior_ms + 1)
            except (AttributeError, OSError, OverflowError, ValueError):
                pass
        return now

    def _next_stamp(self, table: str) -> str:
        now = self._next_epoch_ms(table)
        return _stamp(datetime.fromtimestamp(now / 1000, tz=timezone.utc))

    def _insert_marker(self, dataset: str, version: str, state: str, *, row_count: int,
                       sha256: str, source: str, available_at: str, request_count: int,
                       elapsed_ms: int) -> None:
        stamp = self._next_stamp(_META_TABLE)
        values = (stamp, dataset, version, state, row_count, sha256, source,
                  available_at, request_count, elapsed_ms)
        sql_values = ",".join(_literal(value) for value in values)
        self._cursor().execute(
            f"INSERT INTO {self.database}.{_META_TABLE} VALUES ({sql_values})"
        )

    def begin(self, dataset: str, *, source: str, available_at: str) -> str:
        if dataset not in _TABLES:
            raise ValueError("reference_snapshot_dataset_not_allowed")
        self.ensure_schema()
        version = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:12]
        self._insert_marker(dataset, version, "STAGING", row_count=0, sha256="",
                            source=source, available_at=available_at,
                            request_count=0, elapsed_ms=0)
        return version

    def write_and_activate(self, dataset: str, version: str, rows: Iterable[Mapping[str, Any]], *,
                           source: str, available_at: str, request_count: int,
                           elapsed_ms: int) -> dict[str, Any]:
        if dataset not in _TABLES or not re.fullmatch(r"\d{14}-[0-9a-f]{12}", version):
            raise ValueError("reference_snapshot_identity_invalid")
        materialized = [dict(row) for row in rows]
        if not materialized:
            raise ReferenceSnapshotError("reference_snapshot_empty:" + dataset)
        payloads = [payload_json(row) for row in materialized]
        if any(len(payload) > 4096 for payload in payloads):
            raise ReferenceSnapshotError("reference_snapshot_row_too_large:" + dataset)
        digest = rows_sha256(payloads)
        table = _TABLES[dataset]
        base_ms = self._next_epoch_ms(table)
        existing_ids: set[str] = set()
        cursor = self._cursor()
        cursor.execute(f"SELECT row_id FROM {self.database}.{table} WHERE version={_literal(version)}")
        existing_ids.update(str(row[0]) for row in cursor.fetchall())
        rows_to_insert: list[tuple[Any, ...]] = []
        for index, (row, payload) in enumerate(zip(materialized, payloads)):
            row_id = f"{index:08d}{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:56]}"
            if row_id in existing_ids:
                continue
            stamp = _stamp(datetime.fromtimestamp((base_ms + index) / 1000, tz=timezone.utc))
            rows_to_insert.append((stamp, version, row_id, row.get("plate_code"),
                                   row.get("symbol"), source, available_at, payload))
        for offset in range(0, len(rows_to_insert), self.batch_size):
            batch = rows_to_insert[offset:offset + self.batch_size]
            values = ",".join("(" + ",".join(_literal(value) for value in row) + ")" for row in batch)
            cursor.execute(f"INSERT INTO {self.database}.{table} VALUES {values}")

        readback = self._rows_for_version(dataset, version)
        readback_payloads = [payload_json(row["payload"]) for row in readback]
        readback_sha = rows_sha256(readback_payloads)
        if len(readback) != len(materialized) or readback_sha != digest:
            raise ReferenceSnapshotError(
                f"reference_snapshot_readback_mismatch:{dataset}:"
                f"expected={len(materialized)}:{digest}:actual={len(readback)}:{readback_sha}"
            )
        self._insert_marker(dataset, version, "ACTIVE", row_count=len(materialized),
                            sha256=digest, source=source, available_at=available_at,
                            request_count=request_count, elapsed_ms=elapsed_ms)
        active = self.read_latest(dataset)
        if active is None or active["version"] != version:
            raise ReferenceSnapshotError("reference_snapshot_activation_mismatch:" + dataset)
        return {"dataset": dataset, "version": version, "row_count": len(materialized),
                "sha256": digest, "source": source, "available_at": available_at,
                "request_count": request_count, "elapsed_ms": elapsed_ms,
                "readback_row_count": len(readback), "readback_sha256": readback_sha}

    def _rows_for_version(self, dataset: str, version: str) -> list[dict[str, Any]]:
        table = _TABLES[dataset]
        cursor = self._cursor()
        cursor.execute(
            f"SELECT row_id,payload FROM {self.database}.{table} "
            f"WHERE version={_literal(version)} ORDER BY row_id"
        )
        rows = []
        for row_id, payload in cursor.fetchall():
            try:
                parsed = json.loads(str(payload))
            except (TypeError, ValueError) as exc:
                raise ReferenceSnapshotError("reference_snapshot_payload_invalid:" + dataset) from exc
            if not isinstance(parsed, dict):
                raise ReferenceSnapshotError("reference_snapshot_payload_invalid:" + dataset)
            rows.append({"row_id": str(row_id), "payload": parsed})
        return rows

    def read_latest(self, dataset: str) -> dict[str, Any] | None:
        if dataset not in _TABLES:
            raise ValueError("reference_snapshot_dataset_not_allowed")
        cursor = self._cursor()
        try:
            cursor.execute(
                f"SELECT version,row_count,sha256,source,available_at,request_count,elapsed_ms "
                f"FROM {self.database}.{_META_TABLE} WHERE dataset={_literal(dataset)} "
                "AND snapshot_state='ACTIVE' ORDER BY ts DESC LIMIT 1"
            )
            marker = cursor.fetchone()
        except Exception as exc:
            if td_object_missing(exc):
                return None
            raise
        if marker is None:
            return None
        version, row_count, digest, source, available_at, request_count, elapsed_ms = marker
        data = self._rows_for_version(dataset, str(version))
        payloads = [payload_json(item["payload"]) for item in data]
        actual_digest = rows_sha256(payloads)
        if len(data) != int(row_count) or actual_digest != str(digest):
            raise ReferenceSnapshotError("reference_snapshot_active_integrity_failed:" + dataset)
        rows = [item["payload"] for item in data]
        trade_date = next((str(row.get("trade_date") or row.get("date"))
                           for row in rows if row.get("trade_date") or row.get("date")), None)
        return {"dataset": dataset, "version": str(version), "row_count": len(data),
                "sha256": str(digest), "source": str(source), "available_at": str(available_at),
                "request_count": int(request_count or 0), "elapsed_ms": int(elapsed_ms or 0),
                "trade_date": trade_date, "rows": rows}
