#!/usr/bin/env python3
"""Verify TD-first Baostock progress and restart behavior against the live provider."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT / "src", ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
LIVE_DATA_ROOT = Path(os.getenv("CORE_DATA_ROOT", "/home/exedev/core-data")).expanduser()
TD_DATABASE = "core_dev_c590_632551160254"
MIN_SAMPLE = 1500
INTERRUPT_AFTER = 100


def _assert(condition: bool, name: str, detail: Any = None) -> dict[str, Any]:
    return {"name": name, "status": "PASS" if condition else "FAIL", "detail": detail}


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("json_object_required:" + str(path))
    return value


def _catalog_day() -> str:
    from engine_core.data_pipeline.storage import ArtifactCatalog

    latest = (ArtifactCatalog(LIVE_DATA_ROOT).read().get("datasets", {}).get("daily_kline", {})
              .get("latest") or {}).get("trade_date")
    if not isinstance(latest, str):
        raise RuntimeError("daily_kline_latest_trade_date_missing")
    return date.fromisoformat(latest).isoformat()


def _choose_mixed_sample(rows: list[dict[str, Any]], count: int) -> list[str]:
    buckets: dict[str, list[str]] = {}
    for row in rows:
        if str(row.get("trade_status", "")).strip() == "0":
            continue
        raw = str(row.get("symbol") or row.get("code") or "").rsplit(".", 1)[-1]
        symbol = raw.zfill(6) if raw.isdigit() else ""
        if len(symbol) == 6:
            buckets.setdefault(symbol[:3], []).append(symbol)
    for values in buckets.values():
        values[:] = sorted(set(values))
    selected: list[str] = []
    keys = sorted(buckets)
    while len(selected) < count:
        advanced = False
        for key in keys:
            if buckets[key]:
                selected.append(buckets[key].pop(0))
                advanced = True
                if len(selected) == count:
                    break
        if not advanced:
            break
    return selected


def _unused_td_day(store: Any, candidates: list[str]) -> str:
    counts = store.day_counts(candidates)
    for day in candidates:
        if counts.get(day, 0) == 0 and not store.read_versions(day):
            return day
    raise RuntimeError("no_unused_trade_date_in_temp_td")


def _empty_daily_kline_checkpoints(root: Path) -> list[str]:
    checkpoint_root = root / ".checkpoints"
    if not checkpoint_root.exists():
        return []
    matches = []
    for path in checkpoint_root.rglob("*"):
        name = path.name
        if ((name.startswith("daily-kline-td-gap-") and path.is_dir()) or
                (path.is_file() and name.startswith("daily_kline-") and name.endswith(".json"))):
            matches.append(str(path))
    return matches


def _tree_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file() and not path.is_symlink())


def _probe_worker(day: str, symbols_path: Path, root: Path, output: Path, resume_gapfill: bool) -> int:
    from engine_core.data_pipeline.td_sink import DailyKlineTdSink, TaosStore

    store = TaosStore(TD_DATABASE)
    try:
        if resume_gapfill:
            from engine_core.data_pipeline.daily_kline_gapfill import run_td_daily_kline_gapfill

            outcome = run_td_daily_kline_gapfill(
                root, day, store=store, history_min_trading_days=1,
                total_timeout_seconds=6900, no_progress_timeout_seconds=300,
                source_universe_timeout_seconds=300, range_concurrency=1)
            payload = {"outcome": outcome}
        else:
            import baostock as bs
            from engine_core.daily_kline_capture import BaostockClient
            from engine_core.data_pipeline.providers.baostock_daily_provider import BaostockDailyProvider

            symbols = [line.strip() for line in symbols_path.read_text(encoding="ascii").splitlines()
                       if line.strip()]
            sink = DailyKlineTdSink(store, root)
            provider = BaostockDailyProvider(
                client_factory=lambda: BaostockClient(bs), symbols=symbols,
                timeout_seconds=30, retries=2, checkpoint_path=None,
                source_universe_checkpoint=root / ".checkpoints" / f"baostock-source-universe-{day}.json",
                no_progress_timeout_seconds=300, progress_writer=sink)
            fetched = provider.fetch(day)
            payload = {"row_count": len(fetched.get("rows") or []),
                       "resumed_count": fetched.get("resumed_count"),
                       "baostock_timing": fetched.get("baostock_timing"),
                       "source_date_values": fetched.get("source_date_values"),
                       "source_dates_consistent": fetched.get("source_dates_consistent")}
        output.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        return 0
    except Exception as exc:
        output.write_text(json.dumps({"error": f"{type(exc).__name__}:{exc}"}, sort_keys=True) + "\n",
                           encoding="utf-8")
        raise
    finally:
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="recent real trade date; defaults to the latest Core daily_kline date")
    parser.add_argument("--output-dir", help="evidence directory; defaults under fw/evidence/C617")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--worker-date", help=argparse.SUPPRESS)
    parser.add_argument("--symbols-file", help=argparse.SUPPRESS)
    parser.add_argument("--artifact-root", help=argparse.SUPPRESS)
    parser.add_argument("--worker-output", help=argparse.SUPPRESS)
    parser.add_argument("--resume-gapfill", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        return _probe_worker(date.fromisoformat(args.worker_date).isoformat(), Path(args.symbols_file),
                             Path(args.artifact_root), Path(args.worker_output), args.resume_gapfill)

    output = Path(args.output_dir) if args.output_dir else Path(
        "/home/exedev/fw/evidence/C617/accept-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    output.mkdir(parents=True, exist_ok=False)
    result: dict[str, Any] = {"card": "C617", "started_at": datetime.now().astimezone().isoformat(),
                              "checks": [], "facts": {}, "output_dir": str(output)}
    try:
        from examples.rehearse_event import stage_core_data
        from engine_core.data_pipeline.storage import ArtifactCatalog
        from engine_core.data_pipeline.td_sink import TaosStore
        from engine_core.data_pipeline.trade_calendar import TradeCalendarStore

        if not Path(os.environ.get("TD_CONFIG", "")).exists():
            raise RuntimeError("TD_CONFIG_missing")
        day = date.fromisoformat(args.date).isoformat() if args.date else _catalog_day()
        live_catalog = ArtifactCatalog(LIVE_DATA_ROOT)
        live_datasets = live_catalog.read().get("datasets", {})
        universe_dates = set(live_datasets.get("symbol_universe", {}).get("dates", {}))
        daily_dates = set(live_datasets.get("daily_kline", {}).get("dates", {}))
        sessions = [item for item in TradeCalendarStore(LIVE_DATA_ROOT).all_trading_dates() if item <= day]
        if day not in sessions:
            raise RuntimeError("target_not_in_trade_calendar:" + day)
        store = TaosStore(TD_DATABASE)
        try:
            if args.date is None:
                candidates = [item for item in reversed(sessions[-30:])
                              if item in universe_dates and item in daily_dates]
                day = _unused_td_day(store, candidates)
            if store.day_counts([day]).get(day, 0) != 0 or store.read_versions(day):
                raise RuntimeError("temp_td_target_date_not_empty:" + day)
        finally:
            store.close()
        result["trade_date"] = day

        staged_root = output / "rehearse-event-core-data"
        staging = stage_core_data(LIVE_DATA_ROOT, staged_root)
        catalog = ArtifactCatalog(staged_root)
        universe_path = catalog.locate("symbol_universe", day)
        if universe_path is None:
            raise RuntimeError("symbol_universe_missing_for_sample:" + day)
        daily_path = catalog.locate("daily_kline", day)
        if daily_path is None:
            raise RuntimeError("daily_kline_missing_for_real_sample:" + day)
        universe_rows = {str(row.get("symbol") or row.get("code") or "").rsplit(".", 1)[-1].zfill(6): row
                         for row in catalog.read_rows(universe_path)
                         if str(row.get("trade_status", "")).strip() != "0"}
        active_symbols = set(universe_rows)
        known_real_rows = [row for row in catalog.read_rows(daily_path)
                           if str(row.get("symbol") or row.get("code") or "").rsplit(".", 1)[-1].zfill(6)
                           in active_symbols]
        sample = _choose_mixed_sample(known_real_rows, MIN_SAMPLE)
        if len(sample) < MIN_SAMPLE:
            raise RuntimeError(f"mixed_sample_too_small:{len(sample)}<{MIN_SAMPLE}")
        sample_universe = [universe_rows[symbol] for symbol in sample if symbol in universe_rows]
        if len(sample_universe) != len(sample):
            raise RuntimeError("mixed_sample_not_in_real_universe")
        catalog.commit("symbol_universe", day, sample_universe,
                       {"as_of": day, "sample_scope": "accept_c617", "row_count": len(sample_universe)},
                       source="baostock_query_all_stock")
        symbols_path = output / "mixed-symbols.txt"
        symbols_path.write_text("\n".join(sample) + "\n", encoding="ascii")
        disk_baseline_bytes = _tree_bytes(output)
        result["facts"].update({"staging": staging, "sample_count": len(sample),
                                "sample_symbols_sha256": hashlib.sha256(symbols_path.read_bytes()).hexdigest(),
                                "staged_universe_sample_rows": len(sample_universe),
                                "td_database": TD_DATABASE,
                                "disk_baseline_bytes": disk_baseline_bytes,
                                "isolation": "rehearse_event.stage_core_data + pre-existing core_dev database"})

        first_output = output / "first-run.json"
        env = os.environ.copy()
        env["CORE_DATA_ROOT"] = str(staged_root)
        env["CORE_TD_DATABASE"] = TD_DATABASE
        env["PYTHONPATH"] = os.pathsep.join((str(ROOT / "src"), str(ROOT), env.get("PYTHONPATH", "")))
        command = [sys.executable, str(Path(__file__).resolve()), "--worker", "--worker-date", day,
                   "--symbols-file", str(symbols_path), "--artifact-root", str(staged_root),
                   "--worker-output", str(first_output)]
        child = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE, text=True)
        interrupted_count = 0
        checkpoint_observations: list[list[str]] = []
        try:
            store = TaosStore(TD_DATABASE)
            try:
                deadline = time.monotonic() + 900
                while time.monotonic() < deadline and child.poll() is None:
                    interrupted_count = store.day_counts([day]).get(day, 0)
                    observed_files = _empty_daily_kline_checkpoints(staged_root)
                    if observed_files:
                        checkpoint_observations.append(observed_files)
                    if interrupted_count >= max(INTERRUPT_AFTER, int(0.90 * len(sample))):
                        child.send_signal(signal.SIGTERM)
                        break
                    time.sleep(0.5)
            finally:
                store.close()
            if child.poll() is None:
                child.kill()
            _, first_stderr = child.communicate(timeout=30)
        finally:
            if child.poll() is None:
                child.kill()
        result["facts"].update({"first_run_returncode": child.returncode,
                                "first_run_td_rows_at_interrupt": interrupted_count,
                                "first_run_stderr_tail": first_stderr.splitlines()[-8:],
                                "checkpoint_observations_during_first_run": checkpoint_observations})

        store = TaosStore(TD_DATABASE)
        try:
            partial_count = store.day_counts([day]).get(day, 0)
            partial_versions = store.read_versions(day)
        finally:
            store.close()
        first_files = _empty_daily_kline_checkpoints(staged_root)
        result["checks"].extend([
            _assert(child.returncode == -signal.SIGTERM, "first_run_sigterm", child.returncode),
            _assert(INTERRUPT_AFTER <= partial_count < len(sample), "td_contains_only_partial_progress",
                    {"expected": len(sample), "rows": partial_count}),
            _assert(not partial_versions, "partial_day_has_no_version", len(partial_versions)),
            _assert(not first_files and not checkpoint_observations,
                    "no_checkpoint_files_during_or_after_interrupt",
                    {"after_interrupt": first_files, "during_run": checkpoint_observations}),
        ])

        second_output = output / "second-run.json"
        command[-2:] = ["--worker-output", str(second_output)]
        command.append("--resume-gapfill")
        second_child = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True)
        second_checkpoint_observations: list[list[str]] = []
        second_deadline = time.monotonic() + 2400
        while second_child.poll() is None and time.monotonic() < second_deadline:
            observed_files = _empty_daily_kline_checkpoints(staged_root)
            if observed_files:
                second_checkpoint_observations.append(observed_files)
            time.sleep(0.5)
        if second_child.poll() is None:
            second_child.kill()
        second_stdout, second_stderr_text = second_child.communicate(timeout=30)
        second_stderr = second_stderr_text.splitlines()[-8:]
        provider_result = _load_json(second_output) if second_output.is_file() else {}
        store = TaosStore(TD_DATABASE)
        try:
            final_count = store.day_counts([day]).get(day, 0)
            versions = store.read_versions(day)
            td_rows = store.read_day_rows(day)
        finally:
            store.close()
        manifest_path = catalog.locate("daily_kline", day)
        manifest = _load_json(manifest_path) if manifest_path else {}
        catalog_rows = catalog.read_rows(manifest_path) if manifest_path else []
        digest = hashlib.sha256(b"\n".join(
            __import__("engine_core.data_pipeline.storage", fromlist=["canonical_json"]).canonical_json(row)
            for row in catalog_rows) + b"\n").hexdigest() if catalog_rows else ""
        selected_version = next((item for item in versions if item.get("sha256") == digest), None)
        final_files = _empty_daily_kline_checkpoints(staged_root)
        disk_bytes = max(0, _tree_bytes(output) - disk_baseline_bytes)
        resume_outcome = provider_result.get("outcome") or {}
        target_result = next((item for item in resume_outcome.get("partitions", [])
                              if item.get("trade_date") == day), {})
        result["facts"].update({"resume_returncode": second_child.returncode, "resume_stderr_tail": second_stderr,
                                "resume_stdout_tail": second_stdout.splitlines()[-8:],
                                "checkpoint_observations_during_resume": second_checkpoint_observations,
                                "resume_provider_query_count": resume_outcome.get("range_queries"),
                                "resume_provider_resumed_count": partial_count,
                                "catalog_row_count": len(catalog_rows), "td_row_count": final_count,
                                "catalog_sha256": digest, "td_version_sha256": selected_version.get("sha256")
                                if selected_version else None, "disk_increment_bytes": disk_bytes})
        queries = resume_outcome.get("range_queries")
        resumed_count = partial_count
        result["checks"].extend([
            _assert(second_child.returncode == 0 and not provider_result.get("error"),
                    "resume_run_success", {"exit_code": second_child.returncode,
                                           "stderr": second_stderr, "worker": provider_result}),
            _assert(isinstance(resumed_count, int) and resumed_count == partial_count,
                    "resume_count_matches_td_progress",
                    {"resumed_count": resumed_count, "td_rows_before_resume": partial_count}),
            _assert(isinstance(queries, int) and queries == len(sample) - partial_count and queries < len(sample),
                    "resume_only_requests_unfinished_symbols",
                    {"queries": queries, "sample": len(sample), "already_written": partial_count}),
            _assert(not first_files and not final_files and not checkpoint_observations and
                    not second_checkpoint_observations, "no_daily_kline_checkpoint_files",
                    {"after_interrupt": first_files, "after_resume": final_files,
                     "during_first_run": checkpoint_observations,
                     "during_resume": second_checkpoint_observations}),
            _assert(bool(selected_version) and len(versions) > 0 and target_result.get("td_publish") == "READY" and
                    target_result.get("mode") not in {"RETRYABLE_INCOMPLETE", "DRY_RUN"} and
                    target_result.get("business_status") == "FORMAL",
                    "complete_day_has_version", {"versions": versions, "target_result": target_result}),
            _assert(final_count == len(catalog_rows) == len(td_rows), "catalog_and_td_counts_match",
                    {"catalog": len(catalog_rows), "td": final_count, "readback": len(td_rows)}),
            _assert(bool(digest) and selected_version and selected_version.get("sha256") == digest,
                    "catalog_sha_matches_td_version", {"catalog": digest,
                                                        "td": selected_version.get("sha256") if selected_version else None}),
            _assert(disk_bytes < 200 * 1024 * 1024, "staged_disk_increment_under_200mb", disk_bytes),
            _assert(not provider_result.get("error") and not resume_outcome.get("baostock_logout_error") and
                    not first_stderr.splitlines() and not second_stderr,
                    "no_provider_or_process_warnings", {"error": provider_result.get("error"),
                                                         "logout_error": resume_outcome.get("baostock_logout_error"),
                                                         "first_stderr": first_stderr.splitlines()[-8:],
                                                         "stderr": second_stderr}),
        ])
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}:{exc}"
        result["checks"].append(_assert(False, "acceptance_exception", result["error"]))

    result["status"] = "PASS" if result.get("checks") and all(
        check.get("status") == "PASS" for check in result["checks"]) else "FAIL"
    result["finished_at"] = datetime.now().astimezone().isoformat()
    report = output / "result.json"
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
