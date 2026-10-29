#!/usr/bin/env python3
"""Run one scheduled Guardian event against isolated Core data and a pre-existing core_dev TD database."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import signal
import shutil
import socket
import socketserver
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, BinaryIO
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
SCHEDULE_PATH = ROOT / "src/engine_core/guardian/schedule.json"
LIVE_DATA_ROOT = Path("~/core-data").expanduser()
VENV_PYTHON = Path("/home/exedev/services/engine-next/shared/venv/bin/python")
GO = Path("/bin/go")
DEFAULT_TD_DATABASE = "core_dev_c590_632551160254"
DEFAULT_TD_CONFIG = "/home/exedev/validation/operator-runs/taos-cfg"
STATE_ROOT_MAX_CHARS = 40
MIN_ROOT_FREE_BYTES = 5 * 1024 ** 3
MAX_METADATA_FILE_BYTES = 5 * 1024 ** 2
MAX_DATA_ROOT_BYTES = 50 * 1024 ** 2
MAX_REHEARSAL_ROOT_BYTES = 200 * 1024 ** 2
REHEARSAL_RUN_SOFT_LIMIT_BYTES = MAX_REHEARSAL_ROOT_BYTES - 10 * 1024 ** 2
ROOT_JSON_METADATA = {"catalog.json", "trade_dates.json", "daily_status.json"}
TZ = ZoneInfo("Asia/Shanghai")
TERMINAL_STATES = {
    "SUCCEEDED", "SUCCEEDED_DEGRADED", "FAILED_FINAL", "CIRCUIT_OPEN", "MISSED", "GATE_BLOCKED",
    "SKIPPED", "SKIPPED_NA", "SKIPPED_SATISFIED", "SKIPPED_PHASE", "SKIPPED_DEP",
}
EXPECTED_SUCCESS_STATES = {"SUCCEEDED", "SUCCEEDED_DEGRADED", "SKIPPED_NA", "SKIPPED_SATISFIED"}
TD_VERSION_TABLES = ("daily_kline_versions", "limit_up_pool_versions", "ladder_daily_versions")
READ_ONLY_REDIS_COMMANDS = {
    "AUTH", "CLIENT", "COMMAND", "DBSIZE", "ECHO", "EXISTS", "GET", "GETBIT", "GETRANGE", "HGET",
    "HGETALL", "HEXISTS", "HKEYS", "HLEN", "HMGET", "HSCAN", "HSTRLEN", "HVALS", "INFO", "KEYS",
    "LINDEX", "LLEN", "LRANGE", "MGET", "PING", "PTTL", "RANDOMKEY", "SCARD", "SCAN", "SELECT",
    "SISMEMBER", "SMEMBERS", "SRANDMEMBER", "SSCAN", "STRLEN", "TIME", "TTL", "TYPE", "XINFO",
    "XLEN", "XRANGE", "XREVRANGE", "ZCARD", "ZCOUNT", "ZLEXCOUNT", "ZRANGE", "ZRANGEBYLEX",
    "ZRANGEBYSCORE", "ZRANK", "ZREVRANGE", "ZREVRANGEBYLEX", "ZREVRANGEBYSCORE", "ZREVRANK", "ZSCAN",
    "ZSCORE", "ZMSCORE", "HELLO", "QUIT", "RESET",
}


def load_schedule(path: Path = SCHEDULE_PATH) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") not in {2, 3}:
        raise ValueError("unsupported_guardian_schedule")
    if not isinstance(value.get("events"), list) or not isinstance(value.get("profiles"), dict):
        raise ValueError("invalid_guardian_schedule_shape")
    return value


def validate_state_root_path(path: Path | str) -> None:
    if len(os.fspath(path)) > STATE_ROOT_MAX_CHARS:
        raise ValueError(f"state_root_path_too_long:{len(os.fspath(path))}>{STATE_ROOT_MAX_CHARS}")


def alert_is_dry_run(config: dict[str, Any]) -> bool:
    alert = (config.get("handlers") or {}).get("alert") or {}
    argv = alert.get("argv")
    if not isinstance(argv, list) or "alert" not in argv or "--dry-run" not in argv:
        return False
    return not any(arg in {"--send", "--email-send", "--publish"} for arg in argv)


def data_file_action(relative_path: str | Path, size_bytes: int) -> str:
    """Return the safe staging action for one Core data file path."""
    relative = PurePosixPath(os.fspath(relative_path))
    if relative.is_absolute() or ".." in relative.parts or size_bytes < 0:
        raise ValueError("invalid_core_data_relative_path")
    parts = relative.parts
    if any(part.startswith("trade_date=") for part in parts):
        if relative.name == "manifest.json":
            if size_bytes > MAX_METADATA_FILE_BYTES:
                raise ValueError("partition_manifest_exceeds_5mb")
            return "copy"
        return "link"
    if parts and parts[0] == "trade_calendar":
        return "copy" if size_bytes <= MAX_METADATA_FILE_BYTES else "skip"
    if len(parts) == 2 and relative.name == "latest.json":
        if size_bytes > MAX_METADATA_FILE_BYTES:
            raise ValueError("latest_metadata_exceeds_5mb")
        return "copy"
    if len(parts) == 1 and relative.suffix == ".json":
        if relative.name in ROOT_JSON_METADATA and size_bytes > MAX_METADATA_FILE_BYTES:
            raise ValueError(f"required_metadata_exceeds_5mb:{relative.name}")
        return "copy" if size_bytes <= MAX_METADATA_FILE_BYTES else "skip"
    return "skip"


def _safe_source_path(raw_path: str | Path, source_root: Path) -> tuple[Path, Path]:
    value = Path(raw_path)
    source = (value if value.is_absolute() else source_root / value).resolve(strict=True)
    try:
        relative = source.relative_to(source_root.resolve())
    except ValueError:
        raise ValueError("core_data_reference_escapes_root") from None
    if not source.is_file():
        raise ValueError("core_data_reference_not_file")
    return source, relative


def _rewrite_data_references(value: Any, source_root: Path, target_root: Path, key: str = "") -> Any:
    path_keys = {"manifest", "manifest_uri", "rows_uri", "storage_uri"}
    if isinstance(value, dict):
        return {name: _rewrite_data_references(item, source_root, target_root, str(name))
                for name, item in value.items()}
    if isinstance(value, list):
        return [_rewrite_data_references(item, source_root, target_root, key) for item in value]
    if not isinstance(value, str) or value.startswith(("http://", "https://", "redis://", "rediss://")):
        return value
    source_text = str(source_root.resolve())
    if value == source_text or value.startswith(source_text + os.sep):
        return str(target_root / Path(value).relative_to(source_root.resolve()))
    if key in path_keys and not Path(value).is_absolute():
        candidate = (source_root / value).resolve(strict=False)
        try:
            return str(target_root / candidate.relative_to(source_root.resolve()))
        except ValueError:
            return value
    return value


def _tree_size_bytes(root: Path) -> int:
    total = 0
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(directory)
        for name in [*dirnames, *filenames]:
            path = current / name
            try:
                mode = path.lstat().st_mode
                if stat.S_ISREG(mode) or stat.S_ISLNK(mode):
                    total += path.lstat().st_size
            except OSError:
                continue
    return total


def _require_disk_headroom() -> int:
    free_bytes = shutil.disk_usage("/").free
    if free_bytes < MIN_ROOT_FREE_BYTES:
        raise RuntimeError(f"root_disk_below_5gb:free_bytes={free_bytes}")
    return free_bytes


def _copy_metadata(source: Path, target: Path) -> None:
    if source.stat().st_size > MAX_METADATA_FILE_BYTES:
        raise ValueError(f"metadata_file_exceeds_5mb:{source.name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def _stage_partition_tree(source_root: Path, target_root: Path, partition: Path) -> list[Path]:
    copied_manifests: list[Path] = []
    for directory, dirnames, filenames in os.walk(partition, followlinks=False):
        current = Path(directory)
        dirnames[:] = [name for name in dirnames if not (current / name).is_symlink()]
        for name in filenames:
            source = current / name
            link_target = source
            if source.is_symlink():
                link_target = source.resolve(strict=True)
                try:
                    link_target.relative_to(source_root.resolve())
                except ValueError:
                    raise ValueError("partition_symlink_escapes_core_data") from None
            relative = source.relative_to(source_root.resolve())
            action = data_file_action(relative, source.stat().st_size)
            if action == "skip":
                continue
            target = target_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if action == "copy":
                _copy_metadata(source, target)
                if target.name == "manifest.json":
                    copied_manifests.append(target)
            else:
                target.symlink_to(link_target)
    return copied_manifests


def stage_core_data(source_root: Path, target_root: Path) -> dict[str, int]:
    """Copy mutable metadata and link immutable catalog partitions into a small isolated root."""
    source_root = source_root.resolve(strict=True)
    target_root.mkdir(parents=True, exist_ok=False)
    catalog_source = source_root / "catalog.json"
    if not catalog_source.is_file():
        raise ValueError("core_data_catalog_missing")
    catalog = json.loads(catalog_source.read_text(encoding="utf-8"))
    if not isinstance(catalog, dict) or not isinstance(catalog.get("datasets"), dict):
        raise ValueError("core_data_catalog_invalid")

    for item in source_root.iterdir():
        if item.is_file() and not item.is_symlink():
            action = data_file_action(item.name, item.stat().st_size)
            if action == "copy":
                _copy_metadata(item, target_root / item.name)

    calendar_root = source_root / "trade_calendar"
    if calendar_root.is_dir() and not calendar_root.is_symlink():
        for directory, _dirnames, filenames in os.walk(calendar_root, followlinks=False):
            current = Path(directory)
            for name in filenames:
                source = current / name
                if source.is_symlink():
                    continue
                if data_file_action(source.relative_to(source_root), source.stat().st_size) == "copy":
                    _copy_metadata(source, target_root / source.relative_to(source_root))

    for dataset in catalog["datasets"]:
        latest = source_root / str(dataset) / "latest.json"
        if latest.is_file() and not latest.is_symlink():
            _copy_metadata(latest, target_root / latest.relative_to(source_root))

    manifest_sources: dict[Path, Path] = {}
    for group in catalog["datasets"].values():
        if not isinstance(group, dict):
            continue
        for versions in (group.get("dates") or {}).values():
            entries = versions if isinstance(versions, list) else [versions]
            for entry in entries:
                if not isinstance(entry, dict) or not entry.get("manifest"):
                    continue
                source, relative = _safe_source_path(entry["manifest"], source_root)
                manifest_sources[source] = relative
    if not manifest_sources:
        raise ValueError("core_data_catalog_has_no_manifests")

    staged_manifests: set[Path] = set()
    for source_manifest, relative in manifest_sources.items():
        partition = source_manifest.parent
        staged_manifests.update(_stage_partition_tree(source_root, target_root, partition))
        if relative not in {path.relative_to(target_root) for path in staged_manifests}:
            raise ValueError("catalog_manifest_not_staged")

    for source_manifest, relative in manifest_sources.items():
        target_manifest = target_root / relative
        manifest = json.loads(source_manifest.read_text(encoding="utf-8"))
        rewritten = _rewrite_data_references(manifest, source_root, target_root)
        if not isinstance(rewritten, dict):
            raise ValueError("partition_manifest_invalid")
        rewritten["manifest_uri"] = str(target_manifest)
        target_manifest.write_text(json.dumps(rewritten, ensure_ascii=False, separators=(",", ":")) + "\n",
                                   encoding="utf-8")

    rewritten_catalog = _rewrite_data_references(catalog, source_root, target_root)
    (target_root / "catalog.json").write_text(json.dumps(rewritten_catalog, ensure_ascii=False, separators=(",", ":")) + "\n",
                                                encoding="utf-8")

    for dataset in catalog["datasets"]:
        latest = target_root / str(dataset) / "latest.json"
        if latest.is_file():
            value = json.loads(latest.read_text(encoding="utf-8"))
            value = _rewrite_data_references(value, source_root, target_root)
            latest.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n",
                              encoding="utf-8")

    staged_bytes = _tree_size_bytes(target_root)
    if staged_bytes >= MAX_DATA_ROOT_BYTES:
        raise RuntimeError(f"staged_core_data_exceeds_50mb:bytes={staged_bytes}")
    return {"bytes": staged_bytes, "manifests": len(manifest_sources),
            "linked_partitions": sum(1 for path in target_root.rglob("*") if path.is_symlink())}


def _event_references(value: Any) -> list[str]:
    refs: list[str] = []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        for item in value:
            refs.extend(_event_references(item))
    elif isinstance(value, dict):
        ref = value.get("event_id") or value.get("id")
        if isinstance(ref, str):
            refs.append(ref)
    return refs


def _remap_event_references(value: Any, event_ids: dict[str, str]) -> Any:
    if isinstance(value, str):
        return event_ids.get(value, value)
    if isinstance(value, list):
        return [_remap_event_references(item, event_ids) for item in value]
    if isinstance(value, dict):
        mapped = dict(value)
        for key in ("id", "event_id"):
            if isinstance(mapped.get(key), str):
                mapped[key] = event_ids.get(mapped[key], mapped[key])
        return mapped
    return value


def prepare_schedule(schedule: dict[str, Any], event_id: str, with_events: list[str], run_token: str,
                     now: datetime, minutes: int) -> tuple[dict[str, Any], dict[str, str]]:
    if minutes < 1:
        raise ValueError("minutes_must_be_at_least_1")
    by_id = {str(event.get("event_id")): event for event in schedule["events"]
             if isinstance(event, dict) and event.get("event_id")}
    requested = list(dict.fromkeys([event_id, *with_events]))
    missing_ids = [name for name in requested if name not in by_id]
    if missing_ids:
        raise ValueError("event_not_in_schedule:" + ",".join(missing_ids))

    tag = re.sub(r"[^a-z0-9]", "", str(run_token).lower()).removeprefix("rh")[-10:]
    if not tag:
        raise ValueError("invalid_rehearsal_token")
    event_ids = {name: f"{name}_rh{tag}" for name in requested}
    selected = [copy.deepcopy(by_id[name]) for name in requested]
    missing_dependencies: set[str] = set()
    selected_set = set(requested)
    for event in selected:
        for key in ("depends_on", "after"):
            missing_dependencies.update(ref for ref in _event_references(event.get(key))
                                        if ref not in selected_set)
    if missing_dependencies:
        raise ValueError("missing_dependency_events_use_--with:" + ",".join(sorted(missing_dependencies)))

    trigger = now.astimezone(TZ) + timedelta(minutes=minutes)
    # Keep the archived retry/backoff semantics runnable; e.g. limit_pool_d1
    # has a 30-minute source-ladder backoff after its first attempt.
    deadline = trigger + timedelta(minutes=60)
    if deadline.date() != trigger.date():
        raise ValueError("rehearsal_window_crosses_midnight")
    at_text = trigger.strftime("%H:%M:%S")
    deadline_text = deadline.strftime("%H:%M:%S")
    for event in selected:
        original_id = str(event["event_id"])
        event["event_id"] = event_ids[original_id]
        for key in ("depends_on", "after"):
            if key in event:
                event[key] = _remap_event_references(event[key], event_ids)
        event["at"] = at_text
        event["deadline"] = deadline_text
        if "late_until" in event:
            event["late_until"] = deadline_text
        event.pop("phases", None)

    profiles: dict[str, Any] = {}
    for event in selected:
        name = event.get("profile")
        if name and name in schedule["profiles"]:
            profiles[name] = copy.deepcopy(schedule["profiles"][name])
    prepared = copy.deepcopy(schedule)
    prepared["events"] = selected
    prepared["profiles"] = profiles
    return prepared, event_ids


def event_output_partition(schedule: dict[str, Any], event_id: str, event_date: str,
                           trade_dates: list[str]) -> tuple[str, str]:
    """Resolve the main event's configured output dataset and data date."""
    by_id = {str(event.get("event_id")): event for event in schedule.get("events", [])
             if isinstance(event, dict) and event.get("event_id")}
    event = by_id.get(event_id)
    if event is None:
        raise ValueError("event_not_in_schedule:" + event_id)
    dataset_names = set()
    profile = (schedule.get("profiles") or {}).get(event.get("profile"), {})
    for rung in profile.get("ladder", []) if isinstance(profile, dict) else []:
        args = rung.get("args") if isinstance(rung, dict) else None
        if isinstance(args, dict) and args.get("dataset"):
            dataset_names.add(str(args["dataset"]))
    args = event.get("args") or {}
    if isinstance(args, dict) and args.get("dataset"):
        dataset_names.add(str(args["dataset"]))
    if len(dataset_names) != 1:
        raise ValueError("event_output_dataset_not_unique:" + event_id)

    target = args.get("target") if isinstance(args, dict) else None
    offset = int(event.get("data_date_offset") or 0)
    if target == "event_day" or (target is None and offset == 0):
        data_date = event_date
    elif target == "prev_trade_day" or (target is None and offset == -1):
        previous = [day for day in trade_dates if day < event_date]
        if not previous:
            raise ValueError("previous_trade_date_missing:" + event_date)
        data_date = max(previous)
    else:
        raise ValueError("unsupported_event_output_date:" + event_id)
    return next(iter(dataset_names)), data_date


def hide_catalog_partition(catalog: dict[str, Any], dataset: str,
                           data_date: str) -> tuple[dict[str, Any], int]:
    """Return a catalog copy without one output date; the source catalog is untouched."""
    staged = copy.deepcopy(catalog)
    group = (staged.get("datasets") or {}).get(dataset)
    dates = group.get("dates") if isinstance(group, dict) else None
    if not isinstance(dates, dict):
        raise ValueError("event_output_dataset_missing:" + dataset)
    removed = dates.pop(data_date, None)
    removed_count = len(removed) if isinstance(removed, list) else int(removed is not None)
    latest = group.get("latest")
    if isinstance(latest, dict) and latest.get("trade_date") == data_date:
        remaining_dates = sorted(dates)
        if remaining_dates:
            latest_date = remaining_dates[-1]
            versions = dates[latest_date]
            version = versions[-1] if isinstance(versions, list) and versions else versions
            group["latest"] = {"trade_date": latest_date,
                               "version": version.get("version") if isinstance(version, dict) else None}
        else:
            group["latest"] = None
    return staged, removed_count


def refresh_staged_output(data_root: Path, schedule: dict[str, Any], event_id: str,
                          event_date: str) -> dict[str, Any]:
    trade_dates = json.loads((data_root / "trade_dates.json").read_text(encoding="utf-8"))
    if isinstance(trade_dates, dict):
        trade_dates = trade_dates.get("trading_dates")
    if not isinstance(trade_dates, list) or not all(isinstance(day, str) for day in trade_dates):
        raise ValueError("invalid_trade_dates_metadata")
    dataset, data_date = event_output_partition(schedule, event_id, event_date, trade_dates)
    catalog_path = data_root / "catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    staged, removed_count = hide_catalog_partition(catalog, dataset, data_date)
    catalog_path.write_text(json.dumps(staged, ensure_ascii=False, separators=(",", ":")) + "\n",
                            encoding="utf-8")

    latest_path = data_root / dataset / "latest.json"
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    latest = (staged.get("datasets", {}).get(dataset, {}) or {}).get("latest")
    pointer: dict[str, Any] = {}
    if isinstance(latest, dict) and latest.get("trade_date"):
        versions = (staged["datasets"][dataset].get("dates") or {}).get(latest["trade_date"])
        version_entry = versions[-1] if isinstance(versions, list) and versions else versions
        if isinstance(version_entry, dict):
            pointer = {"trade_date": latest["trade_date"], "version": latest.get("version"),
                       "manifest": version_entry.get("manifest")}
    latest_path.write_text(json.dumps(pointer, ensure_ascii=False, separators=(",", ":")) + "\n",
                           encoding="utf-8")
    return {"dataset": dataset, "data_date": data_date, "hidden_versions": removed_count}


def _json_safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return str(value)


def _resp_line(stream: BinaryIO) -> bytes:
    line = stream.readline(1 << 20)
    if not line or not line.endswith(b"\r\n"):
        raise EOFError("redis_protocol_eof")
    return line


def _read_resp(stream: BinaryIO, depth: int = 0) -> tuple[bytes, bytes, Any]:
    if depth > 32:
        raise ValueError("redis_protocol_depth")
    marker = stream.read(1)
    if not marker:
        raise EOFError("redis_protocol_eof")
    if marker in (b"+", b"-", b":", b",", b"#", b"_", b"("):
        line = _resp_line(stream)
        value = line[:-2]
        return marker + line, marker, value
    if marker in (b"$", b"!", b"="):
        line = _resp_line(stream)
        size = int(line[:-2])
        if size < 0:
            return marker + line, marker, None
        payload = stream.read(size + 2)
        if len(payload) != size + 2:
            raise EOFError("redis_protocol_short_bulk")
        return marker + line + payload, marker, payload[:-2]
    if marker in (b"*", b"~", b">", b"%", b"|"):
        line = _resp_line(stream)
        count = int(line[:-2])
        if count < 0:
            return marker + line, marker, None
        child_count = count * 2 if marker in (b"%", b"|") else count
        raw = marker + line
        values = []
        for _ in range(child_count):
            child_raw, _child_marker, child_value = _read_resp(stream, depth + 1)
            raw += child_raw
            values.append(child_value)
        if marker == b"|":
            following, kind, value = _read_resp(stream, depth + 1)
            return raw + following, kind, value
        return raw, marker, values
    raise ValueError("redis_protocol_marker")


def _redis_command_allowed(command: list[bytes]) -> bool:
    if not command:
        return False
    name = command[0].decode("ascii", "ignore").upper()
    if name not in READ_ONLY_REDIS_COMMANDS:
        return False
    if name == "CLIENT":
        subcommand = command[1].decode("ascii", "ignore").upper() if len(command) > 1 else ""
        return subcommand in {"GETNAME", "ID", "INFO", "SETINFO", "SETNAME"}
    return True


class _ReadOnlyRedisHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server = self.server
        try:
            with socket.create_connection((server.upstream_host, server.upstream_port), timeout=5) as upstream:
                upstream.settimeout(30)
                reader = upstream.makefile("rb")
                writer = upstream.makefile("wb")
                while True:
                    request, kind, value = _read_resp(self.rfile)
                    command = value if kind == b"*" and isinstance(value, list) else []
                    if not _redis_command_allowed(command):
                        name = command[0].decode("ascii", "ignore").upper() if command else "UNKNOWN"
                        server.record_blocked(name)
                        self.wfile.write(f"-READ_ONLY_REHEARSAL {name} blocked\r\n".encode())
                        self.wfile.flush()
                        continue
                    writer.write(request)
                    writer.flush()
                    while True:
                        response, response_kind, _response_value = _read_resp(reader)
                        self.wfile.write(response)
                        self.wfile.flush()
                        if response_kind != b">":
                            break
                    if command and command[0].decode("ascii", "ignore").upper() == "QUIT":
                        break
                reader.close()
                writer.close()
        except (ConnectionError, OSError, EOFError, ValueError):
            return


class ReadOnlyRedisProxy:
    """Forward Redis reads through the guard proxy; reject every data-mutating command locally."""

    def __init__(self, redis_url: str):
        parsed = urlsplit(redis_url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "redis" or host not in {"127.0.0.1", "localhost", "::1"} or parsed.port != 6380:
            raise ValueError("REDIS_URL_must_use_guard_proxy_127.0.0.1:6380")
        self.upstream_host = "127.0.0.1"
        self.upstream_port = 6380
        self.blocked_commands: Counter[str] = Counter()
        self._lock = threading.Lock()
        self._server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _ReadOnlyRedisHandler)
        self._server.daemon_threads = True
        self._server.upstream_host = self.upstream_host
        self._server.upstream_port = self.upstream_port
        self._server.record_blocked = self.record_blocked
        self._thread = threading.Thread(target=self._server.serve_forever, name="rehearsal-redis-ro", daemon=True)
        self._thread.start()
        userinfo = parsed.netloc.rsplit("@", 1)[0] + "@" if "@" in parsed.netloc else ""
        local_netloc = f"{userinfo}127.0.0.1:{self._server.server_address[1]}"
        self.url = urlunsplit((parsed.scheme, local_netloc, parsed.path or "/0", parsed.query, ""))

    def record_blocked(self, command: str) -> None:
        with self._lock:
            self.blocked_commands[command] += 1

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=3)

    def blocked_snapshot(self) -> dict[str, int]:
        with self._lock:
            return dict(self.blocked_commands)


def _sanitize(text: str) -> str:
    text = re.sub(r"(?i)(password|passwd|token|secret|cookie|authorization)(\s*[:=]\s*)[^\s,;]+",
                  r"\1\2[redacted]", text)
    return re.sub(r"redis(?:s)?://[^\s\"']+", "[redis-url redacted]", text)


def _run_checked(command: list[str], *, cwd: Path | None = None, stdout_file: Path | None = None) -> str:
    if stdout_file is None:
        completed = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
        output = completed.stdout
        detail = completed.stderr
    else:
        with stdout_file.open("wb") as stream:
            completed = subprocess.run(command, cwd=cwd, stdout=stream, stderr=subprocess.PIPE, check=False)
        output = ""
        detail = completed.stderr.decode("utf-8", "replace")
    if completed.returncode != 0:
        raise RuntimeError(f"command_failed:{Path(command[0]).name}:{completed.returncode}:{_sanitize(detail[-1200:])}")
    return output.strip()


def _archive_commit(commit: str, root: Path) -> tuple[str, Path]:
    resolved = _run_checked(["git", "rev-parse", "--verify", "--end-of-options", f"{commit}^{{commit}}"], cwd=ROOT)
    archive_file = root / "src.tar"
    archive_root = root / "repo"
    archive_root.mkdir()
    with archive_file.open("wb") as stream:
        completed = subprocess.run(["git", "archive", "--format=tar", resolved], cwd=ROOT,
                                   stdout=stream, stderr=subprocess.PIPE, check=False)
    if completed.returncode != 0:
        detail = _sanitize(completed.stderr.decode("utf-8", "replace")[-1200:])
        raise RuntimeError(f"git_archive_failed:{completed.returncode}:{detail}")
    _run_checked(["tar", "-xf", str(archive_file), "-C", str(archive_root)])
    return resolved, archive_root


def _build_guardiand(archive_root: Path, binary: Path) -> str:
    if not GO.is_file():
        raise RuntimeError("go_binary_missing:/bin/go")
    return _run_checked([str(GO), "build", "-buildvcs=false", "-o", str(binary), "./"],
                        cwd=archive_root / "native/guardiand")


def _run_migrate_and_make_config(binary: Path, archive_root: Path, schedule_path: Path, output: Path,
                                 python: Path, data_root: Path, td_database: str) -> dict[str, Any]:
    migrated = output / "migrated.json"
    config_path = output / "config.json"
    migration_output = _run_checked([str(binary), "migrate", "--from", str(schedule_path), "--to", str(migrated),
                                     "--expand"])
    make_config = archive_root / "native/guardiand/scripts/make_config.py"
    make_output = _run_checked([str(python), str(make_config), str(migrated), str(config_path),
                                str(archive_root), str(python)])
    config = json.loads(config_path.read_text(encoding="utf-8"))
    for handler in (config.get("handlers") or {}).values():
        allowed = list(handler.get("env_allow") or [])
        if "CORE_TD_DATABASE" not in allowed:
            allowed.append("CORE_TD_DATABASE")
        handler["env_allow"] = allowed
    config["calendars"] = {"trading": {"file": str(data_root / "trade_dates.json")}}
    config["alerts"] = {"enabled": True}
    if not alert_is_dry_run(config):
        raise RuntimeError("alert_handler_not_dry_run")
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return {"config_path": str(config_path), "migration_output": migration_output,
            "make_config_output": make_output, "alert_argv": config["handlers"]["alert"]["argv"],
            "td_database_allowlisted": all("CORE_TD_DATABASE" in (h.get("env_allow") or [])
                                            for h in config["handlers"].values())}


def _minimal_child_environment(data_root: Path, home_root: Path, archive_root: Path,
                               td_config: str, td_database: str, redis_url: str) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "HOME": str(home_root),
        "TMPDIR": str(data_root.parent),
        "TMP": str(data_root.parent),
        "TEMP": str(data_root.parent),
        "CORE_DATA_ROOT": str(data_root),
        "TD_CONFIG": td_config,
        "CORE_TD_DATABASE": td_database,
        "PYTHONPATH": str(archive_root / "src") + os.pathsep + str(archive_root),
        "PYTHONHASHSEED": "0",
        "GUARDIAN_DRY_RUN": "0",
        "GUARDIAN_EMAIL_SELF": "0",
        "REDIS_URL": redis_url,
        "CORE_REDIS_URL": redis_url,
    }


def _validate_td_database(database: str) -> str:
    name = str(database or "")
    if not re.fullmatch(r"core_dev_[a-z0-9_]{1,24}", name):
        raise ValueError("td_database_must_be_preexisting_core_dev_database")
    return name


def _td_connection(database: str | None, td_config: str):
    import taos

    return taos.connect(**({"database": database} if database else {}), config=td_config)


def td_database_exists(database: str, td_config: str) -> bool:
    connection = _td_connection(None, td_config)
    try:
        cursor = connection.cursor()
        cursor.execute("SHOW DATABASES")
        return any(str(row[0]).casefold() == database.casefold() for row in cursor.fetchall())
    finally:
        connection.close()


def td_version_snapshot(database: str, td_config: str) -> dict[str, Any]:
    connection = _td_connection(database, td_config)
    try:
        cursor = connection.cursor()
        cursor.execute("SHOW TABLES")
        table_names = {str(row[0]) for row in cursor.fetchall()}
        out: dict[str, Any] = {}
        for table in TD_VERSION_TABLES:
            if table not in table_names:
                continue
            cursor.execute(f"SELECT * FROM {database}.{table}")
            columns = [str(item[0]) for item in (cursor.description or [])]
            rows = [{columns[index]: _json_safe(value) for index, value in enumerate(row)} for row in cursor.fetchall()]
            out[table] = {"row_count": len(rows), "rows": rows}
        return out
    finally:
        connection.close()


def _td_diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    tables: dict[str, Any] = {}
    for table in sorted(set(before) | set(after)):
        old_rows = before.get(table, {}).get("rows", [])
        new_rows = after.get(table, {}).get("rows", [])
        old_count = Counter(json.dumps(row, sort_keys=True, ensure_ascii=False) for row in old_rows)
        new_count = Counter(json.dumps(row, sort_keys=True, ensure_ascii=False) for row in new_rows)
        old_map = {json.dumps(row, sort_keys=True, ensure_ascii=False): row for row in old_rows}
        new_map = {json.dumps(row, sort_keys=True, ensure_ascii=False): row for row in new_rows}
        added = [new_map[key] for key in sorted(new_count - old_count) for _ in range((new_count - old_count)[key])]
        removed = [old_map[key] for key in sorted(old_count - new_count) for _ in range((old_count - new_count)[key])]
        tables[table] = {"before_rows": len(old_rows), "after_rows": len(new_rows), "added": added,
                         "removed": removed}
    return tables


def catalog_snapshot(path: Path) -> dict[str, Any]:
    catalog = json.loads(path.read_text(encoding="utf-8"))
    entries: dict[str, dict[str, Any]] = {}
    datasets = catalog.get("datasets") or {}
    for dataset, group in datasets.items():
        if not isinstance(group, dict):
            continue
        for day, raw_entries in (group.get("dates") or {}).items():
            values = raw_entries if isinstance(raw_entries, list) else [raw_entries]
            for entry in values:
                if not isinstance(entry, dict):
                    continue
                version = str(entry.get("version") or "")
                manifest: dict[str, Any] = {}
                manifest_path = entry.get("manifest")
                if manifest_path:
                    try:
                        manifest_file = Path(str(manifest_path))
                        if not manifest_file.is_absolute():
                            manifest_file = path.parent / manifest_file
                        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
                    except (OSError, ValueError, TypeError):
                        manifest = {}
                key = f"{dataset}/{day}/{version or manifest_path or len(entries)}"
                entries[key] = {
                    "dataset": dataset, "trade_date": day, "version": version,
                    "row_count": entry.get("row_count", manifest.get("row_count")),
                    "source": entry.get("source", manifest.get("source")),
                    "provider": entry.get("provider", manifest.get("provider")),
                    "committed_at": manifest.get("committed_at"),
                    "business_status": entry.get("business_status", manifest.get("business_status")),
                    "quality": entry.get("quality", manifest.get("quality")),
                }
    latest = {name: group.get("latest") for name, group in datasets.items() if isinstance(group, dict)}
    by_dataset = {name: sum(1 for entry in entries.values() if entry["dataset"] == name) for name in datasets}
    return {"schema_version": catalog.get("schema_version"), "partition_count": len(entries),
            "dataset_count": len(datasets), "by_dataset": by_dataset, "latest": latest, "entries": entries}


def _catalog_diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    old, new = before.get("entries", {}), after.get("entries", {})
    added = [new[key] for key in sorted(new.keys() - old.keys())]
    removed = [old[key] for key in sorted(old.keys() - new.keys())]
    changed = [{"before": old[key], "after": new[key]} for key in sorted(old.keys() & new.keys())
               if old[key] != new[key]]
    latest_changed = {key: {"before": before.get("latest", {}).get(key), "after": after.get("latest", {}).get(key)}
                      for key in sorted(set(before.get("latest", {})) | set(after.get("latest", {})))
                      if before.get("latest", {}).get(key) != after.get("latest", {}).get(key)}
    return {"added": added, "removed": removed, "changed": changed, "latest_changed": latest_changed}


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=TZ) if parsed.tzinfo is None else parsed


def explain_live_catalog_change(before: dict[str, Any] | None, after: dict[str, Any] | None,
                               ledger_before: list[bytes] | None, ledger_after: list[bytes],
                               temporary_event_ids: set[str], append_only: bool,
                               started_at: str, finished_at: str) -> dict[str, Any]:
    """Accept a concurrent catalog write only when a successful live Guardian event explains it."""
    if before is None or after is None:
        return {"status": "UNVERIFIABLE", "reason": "catalog_snapshot_unavailable"}
    diff = _catalog_diff(before, after)
    if not any(diff[key] for key in ("added", "removed", "changed", "latest_changed")):
        return {"status": "UNCHANGED"}
    if not append_only or ledger_before is None:
        return {"status": "UNEXPLAINED_CHANGE", "reason": "online_ledger_not_append_only"}
    if any(token.encode() in line for token in temporary_event_ids for line in ledger_after):
        return {"status": "UNEXPLAINED_CHANGE", "reason": "temporary_event_id_in_online_ledger"}
    if diff["removed"]:
        return {"status": "UNEXPLAINED_CHANGE", "reason": "live_catalog_partition_removed"}

    start, end = _parse_timestamp(started_at), _parse_timestamp(finished_at)
    if start is None or end is None:
        return {"status": "UNEXPLAINED_CHANGE", "reason": "run_time_unavailable"}
    successes: list[dict[str, Any]] = []
    for line in ledger_after[len(ledger_before):]:
        try:
            row = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        data = row.get("data") or {}
        state = data.get("state") or {}
        status = state.get("status")
        if row.get("type") == "INSTANCE_STATE" and status in {"SUCCEEDED", "SUCCEEDED_DEGRADED"}:
            stamp = _parse_timestamp(row.get("ts"))
            if stamp is not None and start <= stamp <= end:
                successes.append({"event_id": data.get("event_id"), "instance": data.get("instance"),
                                  "status": status, "ts": row.get("ts"), "timestamp": stamp})

    changed_entries = list(diff["added"])
    changed_entries.extend(item.get("after") or {} for item in diff["changed"])
    if not changed_entries:
        return {"status": "UNEXPLAINED_CHANGE", "reason": "catalog_change_has_no_new_version"}
    if any(not any(entry.get("dataset") == dataset and
                   entry.get("trade_date") == (change.get("after") or {}).get("trade_date") and
                   entry.get("version") == (change.get("after") or {}).get("version")
                   for entry in changed_entries)
           for dataset, change in diff["latest_changed"].items()):
        return {"status": "UNEXPLAINED_CHANGE", "reason": "latest_pointer_changed_without_new_version"}

    explained: list[dict[str, Any]] = []
    for entry in changed_entries:
        committed = _parse_timestamp(entry.get("committed_at"))
        if committed is None or not (start <= committed <= end):
            return {"status": "UNEXPLAINED_CHANGE", "reason": "catalog_commit_outside_rehearsal_window"}
        match = next((event for event in successes
                      if abs((committed - event["timestamp"]).total_seconds()) <= 120), None)
        if match is None:
            return {"status": "UNEXPLAINED_CHANGE", "reason": "no_successful_live_guardian_event_near_commit"}
        explained.append({"dataset": entry.get("dataset"), "trade_date": entry.get("trade_date"),
                          "version": entry.get("version"), "source": entry.get("source"),
                          "committed_at": entry.get("committed_at"),
                          "guardian_event_id": match["event_id"], "guardian_status": match["status"],
                          "guardian_completed_at": match["ts"]})
    return {"status": "EXPLAINED_ONLINE_GUARDIAN", "entries": explained}


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_ledger(path: Path) -> list[bytes]:
    if not path.is_file():
        return []
    return [line for line in path.read_bytes().splitlines(keepends=True) if line.strip()]


def _ledger_event_rows(lines: list[bytes], event_id: str, instance_ids: set[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    parsed: list[dict[str, Any]] = []
    for line in lines:
        try:
            row = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(row, dict):
            parsed.append(row)
    state: dict[str, Any] = {}
    ids = set(instance_ids)
    for row in parsed:
        data = row.get("data") or {}
        if row.get("type") == "INSTANCE_STATE" and data.get("event_id") == event_id:
            if data.get("instance"):
                ids.add(str(data["instance"]))
            state = data.get("state") or {}
    selected: list[dict[str, Any]] = []
    for row in parsed:
        kind = str(row.get("type") or "")
        if not (kind == "INSTANCE_STATE" or kind == "ATTEMPT_END" or kind == "ALERT_OUT" or kind.startswith("TIER_")):
            continue
        data = row.get("data") or {}
        match = data.get("event_id") == event_id or data.get("event") == event_id or data.get("instance") in ids
        if kind == "ALERT_OUT" and not match:
            match = event_id in json.dumps(data, ensure_ascii=False)
        if match:
            selected.append(row)
    return selected, state


def _tail(path: Path, count: int = 60) -> list[str]:
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-count:]
    return [_sanitize(line) for line in lines]


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _temporary_process_groups(root: Path) -> set[int]:
    marker = os.fsencode(str(root.resolve()))
    own_group = os.getpgrp()
    groups: set[int] = set()
    for entry in Path("/proc").glob("[0-9]*"):
        try:
            pid = int(entry.name)
            if pid == os.getpid():
                continue
            command = (entry / "cmdline").read_bytes()
            cwd = (entry / "cwd").resolve()
            if marker not in command and root.resolve() not in cwd.parents and cwd != root.resolve():
                continue
            pgid = os.getpgid(pid)
            if pgid > 1 and pgid != own_group:
                groups.add(pgid)
        except (OSError, PermissionError, ValueError, ProcessLookupError):
            continue
    return groups


def _stop_process_group(proc: subprocess.Popen[bytes] | None, root: Path, grace_seconds: float = 8.0) -> bool:
    pgids = _temporary_process_groups(root)
    if proc is not None:
        pgids.add(proc.pid)
    pgids.discard(os.getpgrp())
    for pgid in pgids:
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline and any(_group_alive(pgid) for pgid in pgids):
        time.sleep(0.1)
    for pgid in pgids:
        if _group_alive(pgid):
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    if proc is not None:
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            return False
    return not any(_group_alive(pgid) for pgid in pgids) and not _temporary_process_groups(root)


def _safety_markdown(checks: list[dict[str, Any]]) -> list[str]:
    return [f"- {check['status']}: {check['name']} — {check['detail']}" for check in checks]


def _write_reports(root: Path, summary: dict[str, Any]) -> tuple[Path, Path]:
    json_path, markdown_path = root / "result.json", root / "result.md"
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    state = summary.get("final_state") or {}
    catalog = summary.get("catalog_diff") or {}
    td_diff = summary.get("td_diff") or {}
    td_changes = ", ".join(
        "{} +{}/-{}".format(name, len(value.get("added", [])), len(value.get("removed", [])))
        for name, value in td_diff.items()
    ) or "none"
    lines = [
        f"# Guardian rehearsal: {summary.get('event', 'unknown')}",
        "",
        f"- Commit: `{summary.get('commit', 'UNKNOWN')}`",
        f"- Temporary event ID: `{summary.get('temporary_event_id', 'UNKNOWN')}`",
        f"- Trigger: `{summary.get('trigger_at', 'UNKNOWN')}`; deadline: `{summary.get('deadline_at', 'UNKNOWN')}`",
        f"- Final status: `{state.get('status', 'NO_STATE')}`; last error: `{state.get('last_error', '') or 'none'}`",
        f"- Temporary directory: `{root}` (retained)",
        f"- Core data root: {summary.get('core_data_bytes_after', 0)} bytes; temp directory: {summary.get('temporary_directory_bytes', 0)} bytes",
        "",
        "## Safety checks",
        *_safety_markdown(summary.get("safety_checks", [])),
        "",
        "## Ledger",
        f"- Event rows collected: {len(summary.get('event_ledger_rows', []))}",
        "",
        "## Data changes",
        f"- Catalog partitions: +{len(catalog.get('added', []))}, ~{len(catalog.get('changed', []))}, -{len(catalog.get('removed', []))}",
        f"- TD version tables with rows: {td_changes}",
        "",
        "## stderr tail",
        "```text",
        *(summary.get("stderr_tail", []) or ["(empty)"]),
        "```",
        "",
    ]
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, markdown_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", required=True, help="event_id from the archived schedule.json")
    parser.add_argument("--minutes", type=int, default=3, help="trigger lead time; default 3 minutes")
    parser.add_argument("--commit", default="HEAD", help="git commit to archive; default HEAD")
    parser.add_argument("--with", dest="with_events", nargs="+", action="append", default=[],
                        help="additional event IDs required as dependencies")
    parser.add_argument("--td-database", default=os.environ.get("CORE_TD_DATABASE") or DEFAULT_TD_DATABASE,
                        help="pre-existing core_dev_* TD database; never created by this tool")
    parser.add_argument("--timeout-minutes", type=int, default=45, help="terminal-state wait limit; default 45")
    parser.add_argument("--refresh-output", action="store_true",
                        help="hide this event's output date in the temporary catalog to exercise a fresh write")
    parser.add_argument("--keep", action="store_true",
                        help="retain temporary files (all rehearsal directories are retained by policy)")
    return parser


def run_rehearsal(args: argparse.Namespace) -> int:
    disk_free_before = _require_disk_headroom()
    root = Path(tempfile.mkdtemp(prefix="rh", dir="/tmp"))
    for name in ("TMPDIR", "TMP", "TEMP"):
        os.environ[name] = str(root)
    tempfile.tempdir = str(root)
    state_root = root / "s"
    validate_state_root_path(state_root)
    data_root = root / "core-data"
    home_root = root / "h"
    archive_root: Path | None = None
    binary = root / "g"
    proc: subprocess.Popen[bytes] | None = None
    stderr_path = root / "stderr.log"
    stderr_stream = None
    proxy: ReadOnlyRedisProxy | None = None
    live_ledger_path: Path | None = None
    live_ledger_before: list[bytes] | None = None
    live_catalog_path = LIVE_DATA_ROOT / "catalog.json"
    live_catalog_sha_before: str | None = None
    live_catalog_snapshot_before: dict[str, Any] | None = None
    temp_catalog_before: dict[str, Any] | None = None
    td_before: dict[str, Any] | None = None
    td_config = os.environ.get("TD_CONFIG") or DEFAULT_TD_CONFIG
    td_database = _validate_td_database(args.td_database)
    today = datetime.now(TZ).date().isoformat()
    summary: dict[str, Any] = {
        "event": args.event, "with_events": [item for group in args.with_events for item in group],
        "root": str(root), "state_root": str(state_root), "state_root_chars": len(str(state_root)),
        "temporary_directory_retained": True, "keep_flag": bool(args.keep), "td_database": td_database,
        "trade_date": today, "started_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "root_free_bytes_before": disk_free_before,
        "safety_checks": [], "event_ledger_rows": [], "stderr_tail": [],
    }
    process_stopped = True
    trigger_at = deadline_at = ""
    event_ids: dict[str, str] = {}
    redis_url = os.environ.get("REDIS_URL") or os.environ.get("CORE_REDIS_URL")
    try:
        if not VENV_PYTHON.is_file():
            raise RuntimeError("venv_python_missing")
        if not Path(td_config).exists():
            raise RuntimeError("TD_CONFIG_missing")
        if not LIVE_DATA_ROOT.is_dir() or not live_catalog_path.is_file():
            raise RuntimeError("live_core_data_or_catalog_missing")
        if not redis_url:
            raise RuntimeError("REDIS_URL_missing")

        resolved_commit, archive_root = _archive_commit(args.commit, root)
        summary["commit"] = resolved_commit
        if _tree_size_bytes(root) >= MAX_REHEARSAL_ROOT_BYTES:
            raise RuntimeError("temporary_directory_exceeds_200mb_after_archive")
        _build_guardiand(archive_root, binary)
        if _tree_size_bytes(root) >= MAX_REHEARSAL_ROOT_BYTES:
            raise RuntimeError("temporary_directory_exceeds_200mb_after_build")
        staged_data = stage_core_data(LIVE_DATA_ROOT, data_root)
        summary["staged_data"] = staged_data
        summary["core_data_bytes"] = staged_data["bytes"]
        home_root.mkdir()
        (home_root / "core-data").symlink_to(data_root, target_is_directory=True)

        if not td_database_exists(td_database, td_config):
            raise RuntimeError("td_database_not_preexisting")
        archive_schedule = load_schedule(archive_root / "src/engine_core/guardian/schedule.json")
        now = datetime.now(TZ)
        trigger = now + timedelta(minutes=args.minutes)
        trigger_at = trigger.isoformat(timespec="seconds")
        deadline_at = (trigger + timedelta(minutes=60)).isoformat(timespec="seconds")
        prepared, event_ids = prepare_schedule(archive_schedule, args.event,
                                               [item for group in args.with_events for item in group],
                                               root.name, now, args.minutes)
        if args.refresh_output:
            summary["refreshed_output"] = refresh_staged_output(data_root, archive_schedule, args.event, today)
        summary.update({"event_id_map": event_ids, "temporary_event_id": event_ids[args.event],
                        "trigger_at": trigger_at, "deadline_at": deadline_at})
        staged_schedule = root / "schedule.json"
        staged_schedule.write_text(json.dumps(prepared, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        config_result = _run_migrate_and_make_config(binary, archive_root, staged_schedule, root, VENV_PYTHON,
                                                      data_root, td_database)
        summary["config"] = config_result
        proxy = ReadOnlyRedisProxy(redis_url)
        child_env = _minimal_child_environment(data_root, home_root, archive_root, td_config, td_database, proxy.url)
        summary["email_self_env"] = child_env["GUARDIAN_EMAIL_SELF"]

        live_ledger_path = LIVE_DATA_ROOT / "guardian-native/state/ledger" / f"{today}.jsonl"
        live_ledger_before = _read_ledger(live_ledger_path)
        live_catalog_sha_before = _sha256_file(live_catalog_path)
        live_catalog_snapshot_before = catalog_snapshot(live_catalog_path)
        temp_catalog_before = catalog_snapshot(data_root / "catalog.json")
        td_before = td_version_snapshot(td_database, td_config)
        initial_temp_bytes = _tree_size_bytes(root)
        summary["temporary_directory_bytes_before_run"] = initial_temp_bytes
        if initial_temp_bytes >= REHEARSAL_RUN_SOFT_LIMIT_BYTES:
            raise RuntimeError(f"temporary_directory_exceeds_200mb_before_run:bytes={initial_temp_bytes}")
        stderr_stream = stderr_path.open("wb")
        command = [str(binary), "run", "--config", config_result["config_path"], "--state-root", str(state_root),
                   "--mode", "live"]
        summary["runtime_command"] = command
        proc = subprocess.Popen(command, cwd=str(archive_root), env=child_env, stdout=subprocess.DEVNULL,
                                stderr=stderr_stream, start_new_session=True)
        local_ledger = state_root / "ledger" / f"{today}.jsonl"
        wait_until = time.monotonic() + max(1, args.timeout_minutes) * 60
        final_state: dict[str, Any] = {}
        while time.monotonic() < wait_until:
            temp_bytes = _tree_size_bytes(root)
            summary["temporary_directory_bytes_peak"] = max(
                temp_bytes, int(summary.get("temporary_directory_bytes_peak", 0)))
            if temp_bytes >= REHEARSAL_RUN_SOFT_LIMIT_BYTES:
                summary["error"] = f"temporary_directory_exceeds_200mb:bytes={temp_bytes}"
                break
            if shutil.disk_usage("/").free < MIN_ROOT_FREE_BYTES:
                summary["error"] = "root_disk_below_5gb_during_rehearsal"
                break
            if proc.poll() is not None:
                summary["process_exit_before_terminal"] = proc.returncode
                break
            rows, final_state = _ledger_event_rows(_read_ledger(local_ledger), event_ids[args.event],
                                                  {event_ids[args.event]})
            if final_state.get("status") in TERMINAL_STATES:
                time.sleep(1)
                break
            time.sleep(1)
        summary["timed_out"] = final_state.get("status") not in TERMINAL_STATES
    except Exception as exc:  # noqa: BLE001 - preserve a reviewable report for setup and runtime failures.
        summary["error"] = _sanitize(f"{type(exc).__name__}:{exc}")
    finally:
        process_stopped = _stop_process_group(proc, root)
        if stderr_stream is not None:
            stderr_stream.close()
        if proxy is not None:
            summary["blocked_redis_commands"] = proxy.blocked_snapshot()
            proxy.close()
        if archive_root is not None and "event_ids" in locals() and args.event in event_ids:
            local_ledger = state_root / "ledger" / f"{today}.jsonl"
            instance_ids = {event_ids[args.event]}
            event_rows, final_state = _ledger_event_rows(_read_ledger(local_ledger), event_ids[args.event], instance_ids)
            summary["event_ledger_rows"] = event_rows
            summary["final_state"] = final_state
            summary["ledger_row_count"] = len(_read_ledger(local_ledger))
        summary["stderr_tail"] = _tail(stderr_path)
        live_ledger_after = _read_ledger(live_ledger_path) if live_ledger_path is not None else []
        live_catalog_sha_after = _sha256_file(live_catalog_path) if live_catalog_sha_before is not None else None
        live_ledger_prefix = (live_ledger_before is not None and
                              len(live_ledger_after) >= len(live_ledger_before) and
                              live_ledger_after[:len(live_ledger_before)] == live_ledger_before)
        unique_ids = set(event_ids.values())
        temp_ids_absent = all(not any(token.encode() in line for token in unique_ids) for line in live_ledger_after)
        live_catalog_unchanged = live_catalog_sha_before is not None and live_catalog_sha_before == live_catalog_sha_after
        live_catalog_snapshot_after: dict[str, Any] | None = None
        try:
            if live_catalog_path.is_file() and live_catalog_snapshot_before is not None:
                live_catalog_snapshot_after = catalog_snapshot(live_catalog_path)
        except Exception as exc:  # noqa: BLE001
            summary["live_catalog_snapshot_error"] = f"{type(exc).__name__}:{_sanitize(str(exc))}"
        live_catalog_change = explain_live_catalog_change(
            live_catalog_snapshot_before, live_catalog_snapshot_after, live_ledger_before,
            live_ledger_after, unique_ids, bool(live_ledger_prefix), summary.get("started_at", ""),
            datetime.now(TZ).isoformat(timespec="seconds"),
        )
        summary["live_catalog_change"] = live_catalog_change
        if live_catalog_snapshot_before is not None and live_catalog_snapshot_after is not None:
            summary["live_catalog_diff"] = _catalog_diff(live_catalog_snapshot_before,
                                                           live_catalog_snapshot_after)
        if data_root.is_dir() and (data_root / "catalog.json").is_file() and temp_catalog_before is not None:
            try:
                temp_catalog_after = catalog_snapshot(data_root / "catalog.json")
                summary["catalog_before"] = {k: v for k, v in temp_catalog_before.items() if k != "entries"}
                summary["catalog_after"] = {k: v for k, v in temp_catalog_after.items() if k != "entries"}
                summary["catalog_diff"] = _catalog_diff(temp_catalog_before, temp_catalog_after)
            except Exception as exc:  # noqa: BLE001
                summary["catalog_error"] = f"{type(exc).__name__}:{_sanitize(str(exc))}"
        if td_before is not None:
            try:
                td_after = td_version_snapshot(td_database, td_config)
                summary["td_diff"] = _td_diff(td_before, td_after)
                summary["td_snapshot_rows"] = {
                    "before": {table: value.get("row_count", 0) for table, value in td_before.items()},
                    "after": {table: value.get("row_count", 0) for table, value in td_after.items()},
                }
                summary["td_database_exists_after"] = td_database_exists(td_database, td_config)
            except Exception as exc:  # noqa: BLE001
                summary["td_snapshot_error"] = f"{type(exc).__name__}:{_sanitize(str(exc))}"
        blocked = summary.get("blocked_redis_commands") or {}
        summary["online_ledger"] = {"path": str(live_ledger_path) if live_ledger_path else None,
                                     "before_rows": len(live_ledger_before or []), "after_rows": len(live_ledger_after),
                                     "rows_added": max(0, len(live_ledger_after) - len(live_ledger_before or [])),
                                     "append_only_prefix": bool(live_ledger_prefix),
                                     "temporary_event_ids_absent": bool(temp_ids_absent)}
        summary["live_catalog_sha256"] = {"before": live_catalog_sha_before, "after": live_catalog_sha_after}
        summary["process_stopped"] = process_stopped
        summary["redis_blocked_command_count"] = sum(blocked.values())
        root_free_after = shutil.disk_usage("/").free
        data_bytes_after = _tree_size_bytes(data_root) if data_root.is_dir() else 0
        temp_bytes_after = _tree_size_bytes(root)
        summary["root_free_bytes_after"] = root_free_after
        summary["core_data_bytes_after"] = data_bytes_after
        summary["temporary_directory_bytes"] = temp_bytes_after
        safety = [
            ("state_root_path_length", len(str(state_root)) <= STATE_ROOT_MAX_CHARS,
             f"{len(str(state_root))} chars (limit {STATE_ROOT_MAX_CHARS})"),
            ("root_disk_headroom", disk_free_before >= MIN_ROOT_FREE_BYTES and
             root_free_after >= MIN_ROOT_FREE_BYTES,
             f"free bytes before={disk_free_before}, after={root_free_after}; minimum={MIN_ROOT_FREE_BYTES}"),
            ("core_data_under_50mb", data_bytes_after < MAX_DATA_ROOT_BYTES,
             f"{data_bytes_after} bytes; limit={MAX_DATA_ROOT_BYTES}"),
            ("temporary_directory_under_200mb", temp_bytes_after < MAX_REHEARSAL_ROOT_BYTES,
             f"{temp_bytes_after} bytes; limit={MAX_REHEARSAL_ROOT_BYTES}"),
            ("online_ledger_isolated", bool(live_ledger_prefix and temp_ids_absent),
             f"rows {len(live_ledger_before or [])}->{len(live_ledger_after)}; old rows preserved; rehearsal IDs absent"),
            ("live_catalog_isolated", live_catalog_change.get("status") in {
                "UNCHANGED", "EXPLAINED_ONLINE_GUARDIAN"},
             "sha256 unchanged" if live_catalog_unchanged else
             f"sha256 changed; {live_catalog_change.get('status')} {live_catalog_change.get('entries') or live_catalog_change.get('reason', '')}"),
            ("alerts_are_dry_run", bool((summary.get("config") or {}).get("alert_argv") and
                                         alert_is_dry_run({"handlers": {"alert": {"argv": summary["config"]["alert_argv"]}}}) and
                                         summary.get("email_self_env") == "0"),
             "guardian alert handler uses --dry-run; self-email disabled"),
            ("redis_is_read_only", proxy is not None and not blocked,
             f"local read-only proxy; blocked commands={dict(blocked)}"),
            ("core_data_redirected", data_root.is_dir() and str(data_root) != str(LIVE_DATA_ROOT),
             f"CORE_DATA_ROOT/artifact-root={data_root}"),
            ("td_database_preexisting_temp", bool(td_before is not None and summary.get("td_database_exists_after", True)
                                                     and td_database.startswith("core_dev_")),
             f"existing database={td_database}"),
            ("temporary_process_stopped", bool(process_stopped), "guardian process group stopped"),
        ]
        summary["safety_checks"] = [{"name": name, "status": "PASS" if passed else "FAIL", "detail": detail}
                                     for name, passed, detail in safety]
        state = summary.get("final_state") or {}
        status = state.get("status")
        summary["event_terminal"] = status in TERMINAL_STATES
        summary["event_success"] = status in EXPECTED_SUCCESS_STATES
        summary["finished_at"] = datetime.now(TZ).isoformat(timespec="seconds")
        summary["exit_code"] = 0 if summary["event_success"] and all(x["status"] == "PASS"
                                                                      for x in summary["safety_checks"]) else 1
        try:
            summary["result_json"] = str(root / "result.json")
            summary["result_markdown"] = str(root / "result.md")
            for _ in range(4):
                _write_reports(root, summary)
                measured = _tree_size_bytes(root)
                if summary.get("temporary_directory_bytes") == measured:
                    break
                summary["temporary_directory_bytes"] = measured
            if summary["temporary_directory_bytes"] >= MAX_REHEARSAL_ROOT_BYTES:
                for check in summary["safety_checks"]:
                    if check["name"] == "temporary_directory_under_200mb":
                        check["status"] = "FAIL"
                        check["detail"] = (f"{summary['temporary_directory_bytes']} bytes; "
                                           f"limit={MAX_REHEARSAL_ROOT_BYTES}")
                summary["exit_code"] = 1
            _write_reports(root, summary)
            final_size = _tree_size_bytes(root)
            if summary.get("temporary_directory_bytes") != final_size:
                summary["temporary_directory_bytes"] = final_size
                if final_size >= MAX_REHEARSAL_ROOT_BYTES:
                    for check in summary["safety_checks"]:
                        if check["name"] == "temporary_directory_under_200mb":
                            check["status"] = "FAIL"
                            check["detail"] = f"{final_size} bytes; limit={MAX_REHEARSAL_ROOT_BYTES}"
                    summary["exit_code"] = 1
                _write_reports(root, summary)
        except Exception as exc:  # noqa: BLE001
            summary["report_error"] = _sanitize(f"{type(exc).__name__}:{exc}")
    state = summary.get("final_state") or {}
    safety_line = " ".join(f"{item['name']}={item['status']}" for item in summary.get("safety_checks", []))
    print(f"REHEARSAL event={args.event} status={state.get('status', 'NO_STATE')} "
          f"last_error={state.get('last_error') or 'none'}")
    print(f"SAFETY {safety_line}")
    diff = summary.get("catalog_diff") or {}
    print(f"CATALOG added={len(diff.get('added', []))} changed={len(diff.get('changed', []))} "
          f"removed={len(diff.get('removed', []))}")
    for name, value in sorted((summary.get("td_diff") or {}).items()):
        print(f"TD table={name} added={len(value.get('added', []))} removed={len(value.get('removed', []))}")
    if summary.get("error"):
        print(f"ERROR {summary['error']}")
    print(f"RESULT_JSON={root / 'result.json'}")
    print(f"RESULT_MARKDOWN={root / 'result.md'}")
    return int(summary.get("exit_code", 1))


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.minutes < 1:
        parser.error("--minutes must be at least 1")
    if args.timeout_minutes < 1:
        parser.error("--timeout-minutes must be at least 1")
    try:
        return run_rehearsal(args)
    except (ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
