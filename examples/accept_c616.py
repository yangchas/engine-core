#!/usr/bin/env python3
"""Exercise C616 against real Baostock and isolated Core/TD state."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT / "src", ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
VENV_PYTHON = Path("/home/exedev/services/engine-next/shared/venv/bin/python")
LIVE_DATA_ROOT = Path(os.getenv("CORE_DATA_ROOT", "/home/exedev/core-data")).expanduser()
TD_DATABASE = "core_dev_c590_632551160254"
MIN_RESUME_SYMBOLS = 1500


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("json_object_required:" + str(path))
    return value


def _select_day(requested: str | None) -> str:
    if requested:
        return date.fromisoformat(requested).isoformat()
    from engine_core.data_pipeline.storage import ArtifactCatalog

    latest = (ArtifactCatalog(LIVE_DATA_ROOT).read().get("datasets", {})
              .get("daily_kline", {}).get("latest") or {}).get("trade_date")
    if not isinstance(latest, str):
        raise RuntimeError("daily_kline_latest_trade_date_missing")
    return date.fromisoformat(latest).isoformat()


def _assert(condition: bool, name: str, detail: Any = None) -> dict[str, Any]:
    return {"name": name, "status": "PASS" if condition else "FAIL", "detail": detail}


def _run_rehearsal(day: str, output: Path, timeout_minutes: int,
                   window_minutes: int) -> tuple[dict[str, Any], list[str]]:
    args = {
        "baostock_budget_ratio": 1.0,
        "baostock_no_progress_seconds": 300,
    }
    cmd = [str(VENV_PYTHON), str(ROOT / "examples/rehearse_event.py"),
           "--event", "baostock_probe_1730", "--minutes", "1",
           "--timeout-minutes", str(timeout_minutes),
           "--window-minutes", str(window_minutes),
           "--td-database", TD_DATABASE, "--refresh-output",
           "--event-args-json", json.dumps(args, separators=(",", ":")),
           "--commit", "HEAD"]
    proc = subprocess.run(cmd, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=timeout_minutes * 60 + 180)
    (output / "rehearse_event.log").write_text(proc.stdout, encoding="utf-8")
    result_path = next((line.partition("=")[2] for line in proc.stdout.splitlines()
                        if line.startswith("RESULT_JSON=")), None)
    if not result_path:
        raise RuntimeError(f"rehearse_event_result_missing:exit={proc.returncode}")
    result = _load_json(Path(result_path))
    result["process_exit_code"] = proc.returncode
    return result, cmd


def _choose_mixed_sample(rows: list[dict[str, Any]], count: int) -> list[str]:
    buckets: dict[str, list[str]] = {}
    for row in rows:
        if str(row.get("trade_status", "")).strip() == "0":
            continue
        symbol = str(row.get("symbol") or row.get("code") or "").rsplit(".", 1)[-1].zfill(6)
        if len(symbol) != 6 or not symbol.isdigit():
            continue
        bucket = symbol[:3]
        buckets.setdefault(bucket, []).append(symbol)
    for values in buckets.values():
        values.sort()
    selected: list[str] = []
    keys = sorted(buckets)
    while len(selected) < count:
        progressed = False
        for key in keys:
            if buckets[key]:
                selected.append(buckets[key].pop(0))
                progressed = True
                if len(selected) == count:
                    break
        if not progressed:
            break
    return selected


def _attempt_from_stdout(stdout: str) -> tuple[dict[str, Any], dict[str, Any]]:
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("sample_probe_output_missing")
    result = json.loads(lines[-1])
    detail = result.get("detail") if isinstance(result.get("detail"), dict) else {}
    attempts = detail.get("provider_attempts") if isinstance(detail.get("provider_attempts"), list) else []
    attempt = next((item for item in reversed(attempts)
                   if isinstance(item, dict) and item.get("provider") == "baostock"), {})
    return result, attempt


def _run_resume_sample(day: str, output: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from examples.rehearse_event import stage_core_data
    from engine_core.data_pipeline.storage import ArtifactCatalog

    sample_root = output / "resume-core-data"
    stage_core_data(LIVE_DATA_ROOT, sample_root)
    catalog = ArtifactCatalog(sample_root)
    universe_path = catalog.locate("symbol_universe", day)
    if universe_path is None:
        raise RuntimeError("symbol_universe_missing_for_resume_sample:" + day)
    universe = catalog.read_rows(universe_path)
    sample = _choose_mixed_sample(universe, MIN_RESUME_SYMBOLS)
    if len(sample) < MIN_RESUME_SYMBOLS:
        raise RuntimeError(f"resume_sample_too_small:{len(sample)}<{MIN_RESUME_SYMBOLS}")
    symbols_path = output / "resume-symbols.txt"
    symbols_path.write_text("\n".join(sample) + "\n", encoding="ascii")
    token = hashlib.sha256(symbols_path.read_bytes()).hexdigest()[:12]
    checkpoint = sample_root / ".checkpoints" / f"daily_kline-{day}-sample-{token}.json"
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join((str(ROOT / "src"), str(ROOT), env.get("PYTHONPATH", "")))
    cmd = [str(VENV_PYTHON), str(ROOT / "examples/core_data_daily.py"),
           "--phase", "postmarket", "--date", day,
           "--symbols-file", str(symbols_path), "--artifact-root", str(sample_root),
           "--baostock-no-progress-seconds", "300"]
    first = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 900
    completed_before_signal = 0
    while time.monotonic() < deadline and first.poll() is None:
        try:
            payload = _load_json(checkpoint)
            completed_before_signal = len(payload.get("rows", []))
        except (OSError, ValueError):
            pass
        if completed_before_signal >= 100:
            first.send_signal(signal.SIGTERM)
            break
        time.sleep(0.5)
    if first.poll() is None:
        first.kill()
    first_out, first_err = first.communicate(timeout=30)
    (output / "resume_first.stdout").write_text(first_out, encoding="utf-8")
    (output / "resume_first.stderr").write_text(first_err, encoding="utf-8")
    try:
        checkpoint_after_signal = _load_json(checkpoint)
    except (OSError, ValueError):
        checkpoint_after_signal = {}
    checkpoint_count = len(checkpoint_after_signal.get("rows", []))

    second = subprocess.run(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, timeout=1800)
    (output / "resume_second.stdout").write_text(second.stdout, encoding="utf-8")
    (output / "resume_second.stderr").write_text(second.stderr, encoding="utf-8")
    result, attempt = _attempt_from_stdout(second.stdout)
    total = attempt.get("expected_symbols")
    resumed = attempt.get("resumed_count")
    completed = attempt.get("completed_symbols")
    queries = attempt.get("query_count")
    retry_margin = max(5, len(sample) // 100)
    checks = [
        _assert(first.returncode == -signal.SIGTERM, "first_run_sigterm", first.returncode),
        _assert(checkpoint_count >= 100, "checkpoint_saved_before_sigterm", checkpoint_count),
        _assert(second.returncode == 10 and result.get("status") == "degraded",
                "resumed_sample_completed", {"exit_code": second.returncode, "status": result.get("status")}),
        _assert(total == len(sample) and completed == len(sample), "sample_all_symbols_completed",
                {"expected": len(sample), "provider_expected": total, "completed": completed}),
        _assert(isinstance(resumed, int) and resumed >= checkpoint_count,
                "completed_symbols_not_requeried", {"checkpoint": checkpoint_count, "resumed": resumed}),
        _assert(isinstance(queries, int) and queries <= len(sample) - checkpoint_count + retry_margin,
                "resume_query_count_bounded", {"queries": queries, "sample": len(sample),
                                                 "checkpoint": checkpoint_count, "retry_margin": retry_margin}),
        _assert(isinstance(result.get("coverage"), (int, float)) and result["coverage"] >= 0.98,
                "sample_coverage", result.get("coverage")),
    ]
    details = {"sample_count": len(sample), "symbols_sha256": hashlib.sha256(
        symbols_path.read_bytes()).hexdigest(), "checkpoint_count_at_sigterm": checkpoint_count,
        "provider_attempt": attempt, "exit_code": second.returncode,
        "elapsed_seconds": round(float(result.get("elapsed_ms") or 0) / 1000, 3)}
    return checks, details


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="recent trading day; defaults to Core daily_kline latest")
    parser.add_argument("--timeout-minutes", type=int, default=145)
    parser.add_argument("--window-minutes", type=int, default=130)
    parser.add_argument("--output-dir", help="evidence directory; defaults under fw/evidence/C616")
    args = parser.parse_args(argv)
    result: dict[str, Any] = {"card": "C616", "started_at": datetime.now().astimezone().isoformat(),
                              "checks": [], "commands": {}, "facts": {}}
    output = Path(args.output_dir) if args.output_dir else Path(
        "/home/exedev/fw/evidence/C616/accept-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    output.mkdir(parents=True, exist_ok=False)
    result["output_dir"] = str(output)
    try:
        if not VENV_PYTHON.is_file():
            raise RuntimeError("engine_next_venv_python_missing")
        day = _select_day(args.date)
        result["trade_date"] = day
        if date.today().isoformat() != day:
            raise RuntimeError("rehearsal_event_requires_current_trade_date:" + day)
        rehearsal, cmd = _run_rehearsal(day, output, args.timeout_minutes, args.window_minutes)
        result["commands"]["rehearse_event"] = cmd
        result["rehearsal"] = rehearsal
        checks = result["checks"]
        checks.append(_assert(rehearsal.get("process_exit_code") == 0 and rehearsal.get("event_success"),
                              "full_pull_rehearsal_success", rehearsal.get("final_state")))
        checks.append(_assert(all(item.get("status") == "PASS" for item in rehearsal.get("safety_checks", [])),
                              "rehearsal_safety_checks", rehearsal.get("safety_checks")))
        event_rows = rehearsal.get("event_ledger_rows", [])
        ledger_text = json.dumps(event_rows, ensure_ascii=False)
        checks.append(_assert("FAILED_FINAL" not in ledger_text, "no_failed_final", event_rows))
        alerts = [item for item in event_rows
                  if isinstance(item, dict) and item.get("type") == "ALERT_OUT"]
        checks.append(_assert(not alerts, "no_alerts", alerts))
        root = Path(rehearsal.get("root", ""))
        data_root = root / "core-data"
        from engine_core.data_pipeline.storage import ArtifactCatalog

        catalog = ArtifactCatalog(data_root)
        manifest_path = catalog.locate("daily_kline", day)
        manifest = _load_json(manifest_path) if manifest_path else {}
        quality = manifest.get("quality") if isinstance(manifest.get("quality"), dict) else {}
        expected = quality.get("expected_count")
        rows = catalog.read_rows(manifest_path) if manifest_path else []
        latest = catalog.read().get("datasets", {}).get("daily_kline", {}).get("latest") or {}
        latest_file = _load_json(data_root / "daily_kline/latest.json")
        checks.extend([
            _assert(bool(manifest) and len(rows) == manifest.get("row_count"), "full_partition_readback",
                    {"manifest": str(manifest_path), "rows": len(rows), "row_count": manifest.get("row_count")}),
            _assert(isinstance(quality.get("coverage"), (int, float)) and quality["coverage"] >= 0.995,
                    "full_partition_coverage", {"coverage": quality.get("coverage"), "expected": expected}),
            _assert(latest.get("trade_date") == day and latest.get("version") == manifest.get("version")
                    and latest_file.get("version") == manifest.get("version"),
                    "same_day_latest_pointer", {"catalog": latest, "latest_file": latest_file,
                                                 "manifest_version": manifest.get("version")}),
            _assert(manifest.get("business_status") == "FORMAL", "formal_partition",
                    {"business_status": manifest.get("business_status"),
                     "field_completeness": manifest.get("field_completeness"),
                     "source_dates": manifest.get("source_dates")}),
        ])
        td_rows = rehearsal.get("td_snapshot_rows", {}).get("after", {})
        td_daily = {key: value for key, value in td_rows.items() if "daily_kline" in key.lower()}
        checks.append(_assert(bool(td_daily) and any(value > 0 for value in td_daily.values()),
                              "td_daily_kline_readback", td_daily))
        attempts = manifest.get("provider_attempts", [])
        timing = next((item for item in attempts if item.get("provider") == "baostock"), {})
        checks.append(_assert(timing.get("completed_symbols") == timing.get("expected_symbols") and
                              isinstance(timing.get("expected_symbols"), int) and
                              timing.get("expected_symbols", 0) > 0,
                              "full_market_symbols_processed",
                              {"completed_symbols": timing.get("completed_symbols"),
                               "expected_symbols": timing.get("expected_symbols"),
                               "query_count": timing.get("query_count")}))
        result["facts"].update({"row_count": len(rows), "expected_count": expected,
                                "coverage": quality.get("coverage"),
                                "business_status": manifest.get("business_status"),
                                "provider_attempt": timing, "manifest": str(manifest_path),
                                "rehearsal_root": str(root)})
        resume_checks, resume_facts = _run_resume_sample(day, output)
        checks.extend(resume_checks)
        result["facts"]["resume_sample"] = resume_facts
    except Exception as exc:  # report all failures as reviewable JSON
        result["error"] = f"{type(exc).__name__}:{exc}"
    result["finished_at"] = datetime.now().astimezone().isoformat()
    result["verdict"] = ("PASS" if result.get("checks") and
                         all(item.get("status") == "PASS" for item in result["checks"]) and
                         not result.get("error") else "FAIL")
    if result.get("output_dir"):
        (Path(result["output_dir"]) / "accept_c616.json").write_text(
            json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
