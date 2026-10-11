"""Durable, local-only checkpoints for the TASK-008 parity runner.

Checkpoint files are evidence artifacts, not market inputs. Each batch is
published atomically and exclusively so an interrupted write is ignored on
resume rather than mistaken for a completed batch.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from engine_core import canonical_json, semantic_hash


CHECKPOINT_CONTRACT = "Task008ParityCheckpointV2"
_BATCH_FILENAME = re.compile(r"^batch-(\d{6})\.json$")
_PROCESS_INSTANCE_ID = uuid.uuid4().hex


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )


def _write_bytes_atomic(path: Path, encoded: bytes, *, exclusive: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.tmp-", dir=str(path.parent)
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        if exclusive:
            # A hard link is an atomic create-if-absent on the same filesystem.
            # Unlike replace(), it cannot overwrite evidence from another run.
            os.link(temp_path, path)
        else:
            os.replace(temp_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _write_json_atomic_exclusive(path: Path, value: Any) -> None:
    """Publish one new JSON file atomically without replacing existing evidence."""

    _write_bytes_atomic(path, _json_bytes(value), exclusive=True)


def write_bytes_atomic_exclusive(path: Path, encoded: bytes) -> None:
    """Publish one new byte file atomically without replacing existing evidence."""

    if not isinstance(encoded, bytes):
        raise TypeError("encoded value must be bytes")
    _write_bytes_atomic(path, encoded, exclusive=True)


def _write_json_atomic_replace(path: Path, value: Any) -> None:
    """Atomically replace a mutable progress snapshot, never a checkpoint."""

    _write_bytes_atomic(path, _json_bytes(value), exclusive=False)


write_json_atomic_exclusive = _write_json_atomic_exclusive


def row_hash(row: Mapping[str, Any]) -> str:
    """Hash one symbol's semantic live-node result, excluding its stored hash."""

    if not isinstance(row, Mapping):
        raise TypeError("checkpoint row must be a mapping")
    return semantic_hash({key: value for key, value in row.items() if key != "row_hash"})


def aggregate_row_hash(rows: Mapping[str, Mapping[str, Any]]) -> str:
    """Hash sorted per-symbol row hashes; this is not the live node hash."""

    if not isinstance(rows, Mapping):
        raise TypeError("rows must be a mapping")
    return semantic_hash(
        tuple((str(symbol), str(rows[symbol]["row_hash"])) for symbol in sorted(rows))
    )


class BatchCheckpoint:
    """Append-only-by-batch checkpoint store with a verified hash chain."""

    def __init__(
        self,
        directory: Path,
        *,
        phase: str,
        manifest: Mapping[str, Any],
        expected_symbols: Sequence[str],
        batch_size: int,
        python_hash_seed: str,
        resume: bool,
    ) -> None:
        if not phase or not isinstance(phase, str):
            raise ValueError("checkpoint phase must be a non-empty string")
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
            raise ValueError("batch_size must be a positive integer")
        if not isinstance(python_hash_seed, str) or not python_hash_seed.isdigit():
            raise ValueError("PYTHONHASHSEED must be an explicit decimal integer")
        if not isinstance(manifest, Mapping):
            raise TypeError("checkpoint manifest must be a mapping")
        symbols = tuple(str(symbol) for symbol in expected_symbols)
        if symbols != tuple(sorted(set(symbols))):
            raise ValueError("expected_symbols must be unique and sorted")

        self.directory = Path(directory)
        self.phase = phase
        self.expected_symbols = symbols
        self.batch_size = batch_size
        self.python_hash_seed = python_hash_seed
        self.process_instance_id = _PROCESS_INSTANCE_ID
        self.header = {
            "contract_version": CHECKPOINT_CONTRACT,
            "phase": phase,
            "manifest": dict(manifest),
            "expected_symbols": list(symbols),
            "batch_size": batch_size,
            "python_hash_seed": python_hash_seed,
        }
        self.completed_rows: dict[str, dict[str, Any]] = {}
        self.process_ids: list[int] = []
        self.process_instance_ids: list[str] = []
        self.batch_metrics: list[dict[str, Any]] = []
        self.last_record_hash: str | None = None
        self.batch_count = 0

        header_path = self.directory / "manifest.json"
        if resume:
            if not self.directory.is_dir() or not header_path.is_file():
                raise FileNotFoundError("checkpoint manifest is missing; cannot resume")
            stored_header = json.loads(header_path.read_text(encoding="utf-8"))
            if stored_header != self.header:
                raise ValueError("checkpoint manifest does not match current inputs/configuration")
            self._load_batches()
        else:
            self.directory.mkdir(parents=True, exist_ok=False)
            _write_json_atomic_exclusive(header_path, self.header)

    def _expected_batch_symbols(self, batch_index: int) -> tuple[str, ...]:
        start = batch_index * self.batch_size
        return self.expected_symbols[start : start + self.batch_size]

    def _load_batches(self) -> None:
        batch_files: dict[int, Path] = {}
        for path in self.directory.glob("batch-*.json"):
            match = _BATCH_FILENAME.match(path.name)
            if match is None:
                raise ValueError(f"invalid checkpoint batch filename: {path.name}")
            batch_index = int(match.group(1))
            if batch_index in batch_files:
                raise ValueError("duplicate checkpoint batch index")
            batch_files[batch_index] = path
        if batch_files and sorted(batch_files) != list(range(max(batch_files) + 1)):
            raise ValueError("checkpoint batches contain a gap")
        expected_batch_count = (
            len(self.expected_symbols) + self.batch_size - 1
        ) // self.batch_size
        if any(batch_index >= expected_batch_count for batch_index in batch_files):
            raise ValueError("checkpoint contains a batch outside the expected symbol range")

        previous_hash: str | None = None
        for batch_index in range(len(batch_files)):
            path = batch_files[batch_index]
            record = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(record, Mapping):
                raise ValueError("checkpoint batch must be a JSON object")
            payload = {key: value for key, value in record.items() if key != "record_hash"}
            if record.get("record_hash") != semantic_hash(payload):
                raise ValueError(f"checkpoint batch hash mismatch: {path.name}")
            if record.get("contract_version") != CHECKPOINT_CONTRACT:
                raise ValueError("unsupported checkpoint batch contract")
            if record.get("phase") != self.phase or record.get("batch_index") != batch_index:
                raise ValueError("checkpoint batch phase/index mismatch")
            expected_batch = self._expected_batch_symbols(batch_index)
            if tuple(record.get("symbols", ())) != expected_batch:
                raise ValueError("checkpoint batch symbol list mismatch")
            expected_symbols_hash = hashlib.sha256(
                canonical_json(expected_batch).encode("utf-8")
            ).hexdigest()
            if record.get("symbols_sha256") != expected_symbols_hash:
                raise ValueError("checkpoint batch symbol hash mismatch")
            if record.get("previous_record_hash") != previous_hash:
                raise ValueError("checkpoint hash chain mismatch")
            if record.get("python_hash_seed") != self.python_hash_seed:
                raise ValueError("checkpoint Python hash seed mismatch")
            rows = record.get("rows")
            if not isinstance(rows, list) or len(rows) != len(expected_batch):
                raise ValueError("checkpoint batch row count mismatch")
            for symbol, row in zip(expected_batch, rows):
                if not isinstance(row, Mapping) or row.get("symbol") != symbol:
                    raise ValueError("checkpoint row symbol mismatch")
                if row.get("row_hash") != row_hash(row):
                    raise ValueError(f"checkpoint row hash mismatch for {symbol}")
                if symbol in self.completed_rows:
                    raise ValueError("duplicate symbol in checkpoint")
                self.completed_rows[symbol] = dict(row)
            metrics = record.get("metrics")
            if not isinstance(metrics, Mapping):
                raise ValueError("checkpoint batch metrics must be an object")
            elapsed = metrics.get("batch_elapsed_seconds")
            max_rss = metrics.get("max_rss_kib")
            if (
                isinstance(elapsed, bool)
                or not isinstance(elapsed, (int, float))
                or elapsed < 0
            ):
                raise ValueError("checkpoint batch elapsed time is invalid")
            if max_rss is not None and (
                isinstance(max_rss, bool) or not isinstance(max_rss, int) or max_rss < 0
            ):
                raise ValueError("checkpoint batch max RSS is invalid")
            if not isinstance(metrics.get("completed_at_utc"), str):
                raise ValueError("checkpoint batch completion time is missing")
            fallback_count = metrics.get("fallback_symbol_count", 0)
            error_types = metrics.get("batch_error_types", [])
            if (
                isinstance(fallback_count, bool)
                or not isinstance(fallback_count, int)
                or fallback_count < 0
            ):
                raise ValueError("checkpoint fallback symbol count is invalid")
            if not isinstance(error_types, list) or any(
                not isinstance(value, str) for value in error_types
            ):
                raise ValueError("checkpoint batch error types are invalid")
            self.batch_metrics.append(dict(metrics))
            process_id = record.get("process_id")
            if isinstance(process_id, bool) or not isinstance(process_id, int):
                raise ValueError("checkpoint process_id must be an integer")
            self.process_ids.append(process_id)
            process_instance_id = record.get("process_instance_id")
            if not isinstance(process_instance_id, str) or not process_instance_id:
                raise ValueError("checkpoint process_instance_id must be a non-empty string")
            self.process_instance_ids.append(process_instance_id)
            previous_hash = str(record["record_hash"])
        self.last_record_hash = previous_hash
        self.batch_count = len(batch_files)
        expected_prefix = self.expected_symbols[: len(self.completed_rows)]
        if tuple(self.completed_rows) != expected_prefix:
            raise ValueError("checkpoint contains duplicate, missing, or out-of-order symbols")

    def commit_batch(
        self,
        batch_index: int,
        symbols: Sequence[str],
        rows: Mapping[str, Mapping[str, Any]],
        *,
        metrics: Mapping[str, Any] | None = None,
    ) -> str:
        if isinstance(batch_index, bool) or batch_index != self.batch_count:
            raise ValueError("checkpoint batches must be committed sequentially")
        expected_batch = self._expected_batch_symbols(batch_index)
        if not expected_batch:
            raise ValueError("cannot commit a batch beyond the expected symbol range")
        actual_symbols = tuple(str(symbol) for symbol in symbols)
        if actual_symbols != expected_batch:
            raise ValueError("committed batch symbols do not match the manifest")
        if set(rows) != set(expected_batch):
            raise ValueError("committed batch must contain exactly its expected symbols")
        ordered_rows = []
        for symbol in expected_batch:
            row = rows[symbol]
            if not isinstance(row, Mapping) or row.get("symbol") != symbol:
                raise ValueError(f"row does not identify expected symbol {symbol}")
            if row.get("row_hash") != row_hash(row):
                raise ValueError(f"row hash mismatch for {symbol}")
            ordered_rows.append(dict(row))

        supplied_metrics = dict(metrics or {})
        elapsed = supplied_metrics.get("batch_elapsed_seconds", 0.0)
        max_rss = supplied_metrics.get("max_rss_kib")
        completed_at = supplied_metrics.get("completed_at_utc")
        if (
            isinstance(elapsed, bool)
            or not isinstance(elapsed, (int, float))
            or elapsed < 0
        ):
            raise ValueError("batch_elapsed_seconds must be a non-negative number")
        if max_rss is not None and (
            isinstance(max_rss, bool) or not isinstance(max_rss, int) or max_rss < 0
        ):
            raise ValueError("max_rss_kib must be a non-negative integer or null")
        if completed_at is None:
            completed_at = datetime.now(timezone.utc).isoformat()
        if not isinstance(completed_at, str):
            raise ValueError("completed_at_utc must be a string")
        raw_error_types = supplied_metrics.get("batch_error_types", ())
        if not isinstance(raw_error_types, (tuple, list)) or any(
            not isinstance(value, str) for value in raw_error_types
        ):
            raise ValueError("batch_error_types must be a sequence of strings")
        batch_metrics = {
            "batch_elapsed_seconds": float(elapsed),
            "max_rss_kib": max_rss,
            "completed_at_utc": completed_at,
            "fallback_symbol_count": supplied_metrics.get("fallback_symbol_count", 0),
            "batch_error_types": sorted(set(raw_error_types)),
        }
        fallback_count = batch_metrics["fallback_symbol_count"]
        if isinstance(fallback_count, bool) or not isinstance(fallback_count, int) or fallback_count < 0:
            raise ValueError("fallback_symbol_count must be a non-negative integer")

        symbols_hash = hashlib.sha256(canonical_json(expected_batch).encode("utf-8")).hexdigest()
        payload = {
            "contract_version": CHECKPOINT_CONTRACT,
            "phase": self.phase,
            "batch_index": batch_index,
            "symbols": list(expected_batch),
            "symbols_sha256": symbols_hash,
            "rows": ordered_rows,
            "metrics": batch_metrics,
            "previous_record_hash": self.last_record_hash,
            "process_id": os.getpid(),
            "process_instance_id": self.process_instance_id,
            "python_hash_seed": self.python_hash_seed,
        }
        record = dict(payload)
        record["record_hash"] = semantic_hash(payload)
        _write_json_atomic_exclusive(
            self.directory / f"batch-{batch_index:06d}.json", record
        )
        for symbol in expected_batch:
            self.completed_rows[symbol] = dict(rows[symbol])
        self.process_ids.append(os.getpid())
        self.process_instance_ids.append(self.process_instance_id)
        self.batch_metrics.append(batch_metrics)
        self.last_record_hash = record["record_hash"]
        self.batch_count += 1
        self._write_progress("RUNNING", batch_index=batch_index)
        return self.last_record_hash

    @property
    def progress_path(self) -> Path:
        return self.directory.parent.parent / f"{self.phase}_progress.json"

    def _write_progress(self, phase_status: str, *, batch_index: int | None = None) -> None:
        rss_values = [
            int(item["max_rss_kib"])
            for item in self.batch_metrics
            if item.get("max_rss_kib") is not None
        ]
        _write_json_atomic_replace(
            self.progress_path,
            {
                "contract_version": "Task008ParityProgressV1",
                "phase": self.phase,
                "phase_status": phase_status,
                "completed_batches": self.batch_count,
                "expected_batches": (
                    len(self.expected_symbols) + self.batch_size - 1
                ) // self.batch_size,
                "completed_symbol_count": len(self.completed_rows),
                "expected_symbol_count": len(self.expected_symbols),
                "latest_batch_index": batch_index,
                "process_ids": sorted(set(self.process_ids)),
                "process_instance_ids": sorted(set(self.process_instance_ids)),
                "active_batch_elapsed_seconds": round(
                    sum(float(item["batch_elapsed_seconds"]) for item in self.batch_metrics),
                    3,
                ),
                "max_rss_kib": max(rss_values) if rss_values else None,
                "fallback_symbol_count": sum(
                    int(item.get("fallback_symbol_count", 0)) for item in self.batch_metrics
                ),
                "batch_error_types": sorted(
                    {
                        str(error_type)
                        for item in self.batch_metrics
                        for error_type in item.get("batch_error_types", ())
                    }
                ),
                "last_record_hash": self.last_record_hash,
                "updated_at_utc": datetime.now(timezone.utc).isoformat(),
            },
        )

    def mark_complete(self) -> None:
        if self.batch_count != (len(self.expected_symbols) + self.batch_size - 1) // self.batch_size:
            raise ValueError("cannot mark an incomplete checkpoint phase complete")
        if set(self.completed_rows) != set(self.expected_symbols):
            raise ValueError("cannot mark a checkpoint phase complete with missing symbols")
        if self.progress_path.is_file():
            try:
                existing = json.loads(self.progress_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                existing = None
            if (
                isinstance(existing, Mapping)
                and existing.get("phase_status") == "COMPLETE"
                and existing.get("last_record_hash") == self.last_record_hash
                and existing.get("completed_symbol_count") == len(self.expected_symbols)
            ):
                return
        self._write_progress("COMPLETE", batch_index=self.batch_count - 1)

    def execution_summary(self) -> dict[str, Any]:
        rss_values = [
            int(item["max_rss_kib"])
            for item in self.batch_metrics
            if item.get("max_rss_kib") is not None
        ]
        return {
            "batch_count": self.batch_count,
            "completed_symbol_count": len(self.completed_rows),
            "active_batch_elapsed_seconds": round(
                sum(float(item["batch_elapsed_seconds"]) for item in self.batch_metrics),
                3,
            ),
            "process_ids": sorted(set(self.process_ids)),
            "process_instance_ids": sorted(set(self.process_instance_ids)),
            "python_hash_seed": self.python_hash_seed,
            "max_rss_kib": max(rss_values) if rss_values else None,
            "fallback_symbol_count": sum(
                int(item.get("fallback_symbol_count", 0)) for item in self.batch_metrics
            ),
            "batch_error_types": sorted(
                {
                    str(error_type)
                    for item in self.batch_metrics
                    for error_type in item.get("batch_error_types", ())
                }
            ),
        }


__all__ = [
    "BatchCheckpoint",
    "aggregate_row_hash",
    "row_hash",
    "write_bytes_atomic_exclusive",
    "write_json_atomic_exclusive",
]
