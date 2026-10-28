#!/usr/bin/env python3
"""Mutation check for C587 (intraday Redis checkpoint + startup theme dependency wait).

Each mutation is one literal replacement in a temporary COPY of the tree; the C587 tests (+ gate tests) are run against
the copy and must FAIL.  A surviving mutation is a hole in the tests (exit 1).  The unmutated copy must pass first.

Usage: python examples/mutation_check_c587.py [--only NAME,...]
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = "src/engine_core/data_pipeline/intraday_checkpoint.py"
RUN = "src/engine_core/guardian/intraday_checkpoint_run.py"
GATES = "src/engine_core/guardian/gates.py"
PREP = "src/engine_core/prep_trade_day.py"
SCHEDULE = "src/engine_core/guardian/schedule.json"
HANDLER = "examples/guardian_handler.py"
TESTS = ["tests/test_intraday_checkpoint.py", "tests/test_prep_theme_deps.py", "tests/test_guardian_gates.py"]

MUTATIONS = [
    ("tolerance_upper_bound_removed", CORE, "        elif observed > upper:", "        elif False:"),
    ("before_window_rows_accepted", CORE, "        elif observed < cutoff:", "        elif False:"),
    ("coverage_threshold_removed", CORE, 'if counts["in_window"] >= min_symbols:', 'if counts["in_window"] >= 1:'),
    ("row_date_evidence_removed", CORE, "        elif observed.date().isoformat() != trade_date:", "        elif False:"),
    ("lost_window_reported_as_not_yet", CORE, 'return "passed" if reachable < min_symbols else "not_yet"', 'return "not_yet"'),
    ("source_order_ignored", GATES, "        for source in cfg[\"sources\"]:\n            if source == \"redis_q2\":",
     "        for source in sorted(cfg[\"sources\"]):\n            if source == \"redis_q2\":"),
    ("td_until_ignored_in_gate", GATES, "                if stamp > cfg[\"td_until\"]:", "                if False:"),
    ("td_until_ignored_in_capture", RUN, "                if cutoff > cfg[\"td_until\"]:", "                if False:"),
    ("td_result_not_marked_degraded", RUN, '"degraded": True, "source_dependency": "TD market_data1', '"degraded": False, "source_dependency": "TD market_data1'),
    ("deadline_back_to_old_value", SCHEDULE, '"deadline": "09:36:59"', '"deadline": "09:33:59"'),
    ("dependency_wait_removed", PREP, "                if waited >= budget:", "                if True:"),
    ("live_asof_not_refreshed", PREP, "                if live_default_asof:\n                    # Jobs fetched",
     "                if False:\n                    # Jobs fetched"),
    ("degrade_policy_ignored", PREP, '            elif theme_on_missing == "degrade":', "            elif False:"),
    ("reason_detail_dropped", PREP, '                errors.append("theme_snapshot:PRECOMPUTE_UNAVAILABLE:" + ";".join(diag)[:600])',
     '                errors.append("theme_snapshot:PRECOMPUTE_UNAVAILABLE")'),
    ("late_arrival_not_named", PREP, "    if late:\n        return f\"{dataset}[{day}]:available_at_after_asof", "    if False:\n        return f\"{dataset}[{day}]:available_at_after_asof"),
    ("legacy_failure_fails_event", HANDLER, "        if checkpoint_reply is not None:\n            return 0, {", "        if False:\n            return 0, {"),
    ("gate_bridge_ignores_runtime_start", HANDLER, "    if len(asof) >= 19 and asof[10] == \"T\":", "    if False:"),
]


def run_pytest(tree: Path) -> tuple[int, str]:
    env = {**os.environ, "PYTHONPATH": f"{tree / 'src'}{os.pathsep}{tree}", "PYTHONDONTWRITEBYTECODE": "1"}
    done = subprocess.run([sys.executable, "-m", "pytest", *TESTS, "-q", "-x", "-p", "no:cacheprovider"],
                          cwd=tree, env=env, capture_output=True, text=True, timeout=300)
    tail = [line for line in done.stdout.splitlines() if line.startswith(("FAILED", "ERROR")) or " passed" in line or " failed" in line]
    return done.returncode, "; ".join(tail[-2:])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", default="")
    args = parser.parse_args(argv)
    wanted = {name.strip() for name in args.only.split(",") if name.strip()}
    work = Path(tempfile.mkdtemp(prefix="c587mut-"))
    survivors = 0
    try:
        base = work / "base"
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
        shutil.copytree(ROOT / "src", base / "src", ignore=ignore)
        shutil.copytree(ROOT / "examples", base / "examples", ignore=ignore)
        (base / "tests").mkdir()
        for name in ("conftest.py", *[Path(t).name for t in TESTS]):
            if (ROOT / "tests" / name).is_file():
                shutil.copy(ROOT / "tests" / name, base / "tests" / name)
        shutil.copytree(ROOT / "tests" / "fixtures" / "trade_calendar", base / "tests" / "fixtures" / "trade_calendar")
        code, summary = run_pytest(base)
        print(f"baseline (unmutated copy): {'PASS' if code == 0 else 'FAIL'} {summary}")
        if code != 0:
            return 2
        for name, relative, old, new in MUTATIONS:
            if wanted and name not in wanted:
                continue
            tree = work / name
            shutil.copytree(base, tree)
            path = tree / relative
            text = path.read_text(encoding="utf-8")
            if text.count(old) != 1:
                print(f"  ANCHOR   {name}: expected exactly one occurrence, found {text.count(old)}")
                survivors += 1
                continue
            path.write_text(text.replace(old, new), encoding="utf-8")
            code, summary = run_pytest(tree)
            killed = code != 0
            survivors += not killed
            print(f"  {'KILLED  ' if killed else 'SURVIVED'} {name}: {summary}")
            shutil.rmtree(tree, ignore_errors=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f"survivors={survivors}")
    return 0 if survivors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
