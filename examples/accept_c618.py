#!/usr/bin/env python3
"""Live C618 acceptance: Kaipan refreshes use TD version switching, never checkpoints."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
REHEARSE = ROOT / "examples" / "rehearse_event.py"
DEFAULT_DATABASE = "core_dev_c590_632551160254"
DEFAULT_TD_CONFIG = "/home/exedev/validation/operator-runs/taos-cfg"
MAX_ROOT_BYTES = 200 * 1024 * 1024


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--td-database", default=DEFAULT_DATABASE)
    parser.add_argument("--timeout-minutes", type=int, default=30)
    parser.add_argument("--output", type=Path,
                        default=Path("/home/exedev/fw/evidence/C618/accept_c618.json"))
    return parser


def _connect(database: str | None = None):
    import taos

    config = os.environ.get("TD_CONFIG") or DEFAULT_TD_CONFIG
    return taos.connect(**({"database": database} if database else {}), config=config)


def _database_exists(database: str) -> bool:
    connection = _connect()
    try:
        cursor = connection.cursor()
        cursor.execute("SHOW DATABASES")
        return any(str(row[0]).casefold() == database.casefold() for row in cursor.fetchall())
    finally:
        connection.close()


def _fetchone(database: str, sql: str):
    connection = _connect(database)
    try:
        cursor = connection.cursor()
        cursor.execute(sql)
        return cursor.fetchone()
    finally:
        connection.close()


def _fetchall(database: str, sql: str):
    connection = _connect(database)
    try:
        cursor = connection.cursor()
        cursor.execute(sql)
        return cursor.fetchall()
    finally:
        connection.close()


def _tables(database: str) -> set[str]:
    connection = _connect(database)
    try:
        cursor = connection.cursor()
        cursor.execute(f"SHOW {database}.tables")
        return {str(row[0]) for row in cursor.fetchall()}
    finally:
        connection.close()


def _active_snapshot(database: str) -> dict[str, Any] | None:
    if "reference_snapshot_versions" not in _tables(database):
        return None
    row = _fetchone(
        database,
        "SELECT version,row_count,sha256,source,available_at,request_count,elapsed_ms "
        "FROM reference_snapshot_versions WHERE dataset='plate_members' AND snapshot_state='ACTIVE' "
        "ORDER BY ts DESC LIMIT 1",
    )
    if row is None:
        return None
    version, row_count, sha256, source, available_at, request_count, elapsed_ms = row
    version = str(version)
    rows = _fetchall(
        database,
        "SELECT plate_code,symbol,source,available_at,payload,version FROM plate_members_latest "
        f"WHERE version='{version}' ORDER BY plate_code,symbol,row_id",
    )
    from engine_core.data_pipeline.reference_snapshot_td import payload_json

    payloads = [payload_json(json.loads(str(item[4]))) for item in rows]
    digest = hashlib.sha256("\n".join(payloads).encode("utf-8")).hexdigest()
    return {
        "version": version,
        "row_count": int(row_count),
        "readback_count": len(rows),
        "sha256": str(sha256),
        "readback_sha256": digest,
        "source": str(source),
        "available_at": str(available_at),
        "request_count": int(request_count or 0),
        "elapsed_ms": int(elapsed_ms or 0),
        "row_versions": sorted({str(item[5]) for item in rows}),
    }


def _staging_after(database: str, started_epoch_ms: int) -> dict[str, Any] | None:
    connection = _connect(database)
    try:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT ts,version,row_count,sha256 FROM reference_snapshot_versions "
            "WHERE dataset='plate_members' AND snapshot_state='STAGING' ORDER BY ts DESC LIMIT 1"
        )
        row = cursor.fetchone()
        if row is None:
            return None
        stamp, version, row_count, sha256 = row
        try:
            stamp_ms = int(stamp.timestamp() * 1000)
        except (AttributeError, TypeError, ValueError):
            stamp_ms = 0
        if stamp_ms < started_epoch_ms:
            return None
        cursor.execute(
            "SELECT row_id FROM plate_members_latest "
            f"WHERE version='{str(version)}' ORDER BY ts DESC LIMIT 1"
        )
        return {
            "version": str(version), "row_count": int(row_count or 0), "sha256": str(sha256 or ""),
            "staged_row_present": cursor.fetchone() is not None,
        }
    finally:
        connection.close()


def _find_checkpoint_files(data_root: Path) -> dict[str, str]:
    checkpoint_root = data_root / ".checkpoints"
    if not checkpoint_root.exists():
        return {}
    result = subprocess.run(
        ["find", str(checkpoint_root), "-type", "f", "-print0"],
        check=True, capture_output=True,
    )
    files: dict[str, str] = {}
    for raw_path in result.stdout.split(b"\0"):
        if not raw_path:
            continue
        path = Path(os.fsdecode(raw_path))
        files[str(path.relative_to(checkpoint_root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def _new_rehearsal_data_root(before: set[Path], process: subprocess.Popen[str], timeout: float) -> Path:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        candidates = sorted(Path("/tmp").glob("rh*/core-data"))
        for candidate in candidates:
            if candidate not in before and (candidate / "catalog.json").is_file():
                return candidate
        if process.poll() is not None:
            break
        time.sleep(0.05)
    raise RuntimeError("rehearsal_temp_data_root_not_found")


def _descendants(pid: int) -> list[int]:
    result: list[int] = []
    pending = [pid]
    while pending:
        parent = pending.pop()
        child_file = Path(f"/proc/{parent}/task/{parent}/children")
        try:
            children = [int(value) for value in child_file.read_text().split()]
        except (OSError, ValueError):
            continue
        result.extend(children)
        pending.extend(children)
    return result


def _guardian_pid(root: Path, rehearsal_pid: int) -> int | None:
    binary = root.parent / "g"
    for pid in _descendants(rehearsal_pid):
        try:
            command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            continue
        if str(binary) in command:
            return pid
    return None


def _run_rehearsal(database: str, commit: str, timeout_minutes: int, *,
                   interrupt_after_staging: bool = False,
                   cleanup_roots: set[Path] | None = None) -> dict[str, Any]:
    temp_roots_before = set(Path("/tmp").glob("rh*"))
    data_roots_before = {path / "core-data" for path in temp_roots_before}
    started = time.monotonic()
    started_epoch_ms = int(time.time() * 1000)
    overrides = {
        "static_data_refresh": {
            "datasets": ["plate_members"],
            "force": True,
            "trigger_time": "08:50",
            "weekly_days": 7,
            "max_age_days": 8,
            "kaipan_max_requests": 400,
        }
    }
    command = [
        sys.executable, str(REHEARSE), "--event", "static_data_refresh_0850",
        "--minutes", "1", "--timeout-minutes", str(timeout_minutes),
        "--td-database", database, "--commit", commit, "--isolate-target",
        "--event-args-json", json.dumps(overrides, separators=(",", ":")),
    ]
    process: subprocess.Popen[str] | None = None
    data_root: Path | None = None
    checkpoint_baseline: dict[str, str] = {}
    checkpoint_baseline_captured = False
    checkpoint_changes: list[dict[str, Any]] = []
    interrupted = False
    interrupt_version: str | None = None
    interrupt_staged_row_present = False
    last_staging_probe = 0.0
    staging_probe_interval_seconds = 2.0
    staging_deadline = time.monotonic() + min(12 * 60, timeout_minutes * 60)
    runner_error: str | None = None
    output = ""
    try:
        process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True)
        while process.poll() is None:
            if data_root is None:
                try:
                    data_root = _new_rehearsal_data_root(data_roots_before, process, 0.1)
                    if cleanup_roots is not None:
                        cleanup_roots.add(data_root.parent)
                except RuntimeError:
                    pass
            if data_root is not None and (data_root / "catalog.json").is_file():
                current = _find_checkpoint_files(data_root)
                if not checkpoint_baseline_captured:
                    checkpoint_baseline = current
                    checkpoint_baseline_captured = True
                elif current != checkpoint_baseline and (
                        not checkpoint_changes or checkpoint_changes[-1]["files"] != current):
                    checkpoint_changes.append({"at_seconds": round(time.monotonic() - started, 3),
                                               "files": current})
            if (interrupt_after_staging and not interrupted
                    and time.monotonic() < staging_deadline
                    and time.monotonic() - last_staging_probe >= staging_probe_interval_seconds):
                last_staging_probe = time.monotonic()
                staged = _staging_after(database, started_epoch_ms)
                if staged is not None and staged["staged_row_present"]:
                    if data_root is None:
                        data_root = _new_rehearsal_data_root(data_roots_before, process, 1.0)
                        if cleanup_roots is not None:
                            cleanup_roots.add(data_root.parent)
                    guardian_pid = _guardian_pid(data_root, process.pid)
                    if guardian_pid is None:
                        raise RuntimeError("guardian_process_not_found_for_sigterm")
                    interrupt_version = staged["version"]
                    interrupt_staged_row_present = True
                    os.kill(guardian_pid, signal.SIGTERM)
                    interrupted = True
            time.sleep(0.1)
    except Exception as exc:
        runner_error = f"{type(exc).__name__}:{exc}"
        if process is not None and process.poll() is None:
            for candidate in Path("/tmp").glob("rh*"):
                if candidate in temp_roots_before:
                    continue
                candidate_data_root = candidate / "core-data"
                guardian_pid = _guardian_pid(candidate_data_root, process.pid)
                if guardian_pid is not None:
                    try:
                        os.kill(guardian_pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    break
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
        if process is not None and process.stdout is not None:
            try:
                output = process.communicate(timeout=5)[0] or ""
            except subprocess.TimeoutExpired:
                output = ""
    if process is not None:
        exit_code = process.returncode
        if process.stdout is not None and not output:
            output = process.communicate()[0] or ""
    else:
        exit_code = -1
    result_path = None
    for line in output.splitlines():
        if line.startswith("RESULT_JSON="):
            result_path = Path(line.partition("=")[2])
    summary: dict[str, Any] = {}
    if result_path is not None and result_path.is_file():
        summary = json.loads(result_path.read_text(encoding="utf-8"))
        if data_root is None and summary.get("root"):
            data_root = Path(str(summary["root"])) / "core-data"
    if data_root is not None and cleanup_roots is not None:
        cleanup_roots.add(data_root.parent)
    if data_root is None:
        candidates = [path / "core-data" for path in Path("/tmp").glob("rh*")
                     if path not in temp_roots_before and (path / "core-data").exists()]
        if candidates:
            data_root = max(candidates, key=lambda path: path.stat().st_mtime)
    final_checkpoint_files = _find_checkpoint_files(data_root) if data_root is not None else {}
    if not checkpoint_baseline_captured:
        checkpoint_baseline = final_checkpoint_files
        checkpoint_baseline_captured = data_root is not None
    if final_checkpoint_files != checkpoint_baseline and (
            not checkpoint_changes or checkpoint_changes[-1]["files"] != final_checkpoint_files):
        checkpoint_changes.append({"at_seconds": round(time.monotonic() - started, 3),
                                   "files": final_checkpoint_files})
    event_rows = summary.get("event_ledger_rows") or []
    alerts = [row for row in event_rows if row.get("type") == "ALERT_OUT"]
    state = summary.get("final_state") or {}
    root_bytes = summary.get("temporary_directory_bytes")
    result = {
        "exit_code": exit_code,
        "status": state.get("status"),
        "error": summary.get("error") or runner_error,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "alert_count": len(alerts),
        "checkpoint_baseline_count": len(checkpoint_baseline),
        "checkpoint_baseline_captured_before_event": checkpoint_baseline_captured,
        "checkpoint_changes": checkpoint_changes,
        "temporary_directory_bytes": root_bytes,
        "temporary_directory_under_200mb": isinstance(root_bytes, int) and root_bytes < MAX_ROOT_BYTES,
        "interrupted_by_sigterm": interrupted,
        "interrupted_version": interrupt_version,
        "interrupted_staged_row_present": interrupt_staged_row_present,
        "rehearsal_root": str(result_path.parent) if result_path else None,
        "stdout_tail": output.splitlines()[-20:],
        "summary": {
            "event_success": summary.get("event_success"),
            "safety_checks": summary.get("safety_checks", []),
            "core_data_bytes_after": summary.get("core_data_bytes_after"),
            "temporary_directory_bytes_peak": summary.get("temporary_directory_bytes_peak"),
            "root_free_bytes_after": summary.get("root_free_bytes_after"),
        },
    }
    if data_root is not None:
        rehearsal_root = data_root.parent
        if rehearsal_root.parent == Path("/tmp") and rehearsal_root.name.startswith("rh"):
            result["_cleanup_root"] = str(rehearsal_root)
    if interrupt_after_staging and not interrupted:
        result["error"] = result["error"] or "sigterm_staging_point_not_reached"
    return result


def _validate_snapshot(snapshot: dict[str, Any] | None, label: str) -> dict[str, Any]:
    if snapshot is None:
        raise RuntimeError(f"{label}:plate_members_active_version_missing")
    if snapshot["row_count"] <= 0 or snapshot["readback_count"] != snapshot["row_count"]:
        raise RuntimeError(f"{label}:plate_members_row_count_mismatch")
    if snapshot["sha256"] != snapshot["readback_sha256"]:
        raise RuntimeError(f"{label}:plate_members_sha256_mismatch")
    if snapshot["row_versions"] != [snapshot["version"]]:
        raise RuntimeError(f"{label}:plate_members_mixed_versions")
    if snapshot["request_count"] <= 0 or snapshot["elapsed_ms"] <= 0:
        raise RuntimeError(f"{label}:provider_measurements_missing")
    return snapshot


def _baseline_snapshot_or_refresh(database: str, commit: str, timeout_minutes: int,
                                  cleanup_roots: set[Path]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    existing = _active_snapshot(database)
    if existing is not None:
        return _validate_snapshot(existing, "baseline"), None

    first = _run_rehearsal(database, commit, timeout_minutes, cleanup_roots=cleanup_roots)
    if first.get("_cleanup_root"):
        cleanup_roots.add(Path(first.pop("_cleanup_root")))
    if first["exit_code"] != 0 or first["status"] not in {"SUCCEEDED", "SUCCEEDED_DEGRADED"}:
        raise RuntimeError("first_static_refresh_rehearsal_failed")
    if first["alert_count"] or first["checkpoint_changes"] or not first["temporary_directory_under_200mb"]:
        raise RuntimeError("first_rehearsal_safety_assertion_failed")
    return _validate_snapshot(_active_snapshot(database), "first"), first


def run(args: argparse.Namespace) -> dict[str, Any]:
    database = str(args.td_database)
    if not database.startswith("core_dev_") or not database.replace("_", "").isalnum():
        raise ValueError("td_database_must_be_preexisting_core_dev_database")
    if not _database_exists(database):
        raise RuntimeError("td_database_not_preexisting")
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                            check=True, capture_output=True, text=True).stdout.strip()
    result: dict[str, Any] = {
        "card": "C618", "td_database": database, "commit": commit,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "runs": [], "status": "FAIL",
    }
    cleanup_roots: set[Path] = set()
    try:
        from engine_core.data_pipeline.reference_snapshot_td import ReferenceSnapshotStore

        with ReferenceSnapshotStore(database=database) as store:
            store.ensure_schema()
        first_snapshot, first = _baseline_snapshot_or_refresh(
            database, commit, args.timeout_minutes, cleanup_roots
        )
        result["initial_snapshot_reused"] = first is None
        if first is not None:
            result["runs"].append(first)

        interrupted = _run_rehearsal(database, commit, args.timeout_minutes,
                                     interrupt_after_staging=True,
                                     cleanup_roots=cleanup_roots)
        if interrupted.get("_cleanup_root"):
            cleanup_roots.add(Path(interrupted.pop("_cleanup_root")))
        result["runs"].append(interrupted)
        if not interrupted["interrupted_by_sigterm"]:
            raise RuntimeError("sigterm_rehearsal_not_interrupted")
        if not interrupted["interrupted_staged_row_present"]:
            raise RuntimeError("sigterm_did_not_interrupt_td_row_write")
        if interrupted["checkpoint_changes"]:
            raise RuntimeError("sigterm_rehearsal_created_checkpoint_files")
        after_sigterm = _validate_snapshot(_active_snapshot(database), "after_sigterm")
        if after_sigterm["version"] not in {first_snapshot["version"], interrupted["interrupted_version"]}:
            raise RuntimeError("sigterm_left_unexpected_active_version")

        final = _run_rehearsal(database, commit, args.timeout_minutes,
                               cleanup_roots=cleanup_roots)
        if final.get("_cleanup_root"):
            cleanup_roots.add(Path(final.pop("_cleanup_root")))
        result["runs"].append(final)
        if final["exit_code"] != 0 or final["status"] not in {"SUCCEEDED", "SUCCEEDED_DEGRADED"}:
            raise RuntimeError("post_sigterm_static_refresh_rehearsal_failed")
        if final["alert_count"] or final["checkpoint_changes"] or not final["temporary_directory_under_200mb"]:
            raise RuntimeError("post_sigterm_rehearsal_safety_assertion_failed")
        final_snapshot = _validate_snapshot(_active_snapshot(database), "final")
        if final_snapshot["version"] == first_snapshot["version"]:
            raise RuntimeError("post_sigterm_refresh_did_not_activate_second_version")
        result["plate_members"] = {
            "first_version": first_snapshot,
            "after_sigterm_version": after_sigterm,
            "final_version": final_snapshot,
            "second_version_only_visible": final_snapshot["row_versions"] == [final_snapshot["version"]],
            "fetch_count_matches_td": final_snapshot["row_count"] == final_snapshot["readback_count"],
            "fetch_sha_matches_td": final_snapshot["sha256"] == final_snapshot["readback_sha256"],
        }
        result["status"] = "PASS"
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}:{exc}"
    finally:
        cleaned: list[str] = []
        for path in sorted(cleanup_roots):
            if path.parent == Path("/tmp") and path.name.startswith("rh") and path.exists():
                shutil.rmtree(path)
                cleaned.append(str(path))
        result["temporary_roots_cleaned"] = cleaned
    result["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                           encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = run(args)
    except Exception as exc:
        result = {"card": "C618", "status": "FAIL", "error": f"{type(exc).__name__}:{exc}"}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if result.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
