#!/usr/bin/env python3
"""Mutation check of the D-1 limit-up pool (C580, acceptance D): break the production code on a COPY, the tests must fail.

Each mutation is a literal text replacement in one source file of a temporary copy of ``src`` (the repository is never
touched).  ``tests/test_limit_pool.py`` is then run against the copy; a mutation is "killed" when pytest fails.  A
mutation that survives is a hole in the tests and the script exits 1.  The unmutated copy must pass first.

Usage: python examples/mutation_check_limit_pool.py [--only NAME,...]
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
CORE = "src/engine_core/data_pipeline/limit_pool.py"
SCHEDULE = "src/engine_core/guardian/schedule.json"
RUNNER = "src/engine_core/guardian/limit_pool_d1.py"
STORAGE = "src/engine_core/data_pipeline/storage.py"

# (name, file, old, new)
MUTATIONS = [
    ("ladder_order_swapped", SCHEDULE, '"name": "kaipan",', '"name": "kaipan_x",'),   # replaced below by a real reorder
    ("kaipan_date_evidence_removed", CORE, "    if bad:\n        raise PoolSourceError(\"TIER_DATE_MISMATCH\"",
     "    if False:\n        raise PoolSourceError(\"TIER_DATE_MISMATCH\""),
    ("wencai_date_evidence_removed", CORE, "    if days != {trade_date}:", "    if False:"),
    ("st_filter_removed", CORE, "        if st_known and is_st_name(name):", "        if False:"),
    ("beijing_board_limit_wrong", CORE, "        return 30.0\n    return 10.0", "        return 20.0\n    return 10.0"),
    ("st_five_percent_tier_added", CORE, "        return 30.0\n    return 10.0",
     "        return 30.0\n    return 5.0 if symbol.startswith((\"60\", \"00\")) else 10.0"),
    ("star_board_limit_dropped", CORE, 'if symbol.startswith(("688", "689", "30")):', 'if symbol.startswith(("688", "689")):'),
    ("td_threshold_margin_wrong", CORE, "TD_RULE_MARGIN_PCT = 0.6", "TD_RULE_MARGIN_PCT = 2.6"),
    ("td_close_equals_high_dropped", CORE, "pct >= limit_pct(symbol) - TD_RULE_MARGIN_PCT and close == high",
     "pct >= limit_pct(symbol) - TD_RULE_MARGIN_PCT"),
    ("td_streak_never_breaks", CORE, "            if not bar_is_limit_up(symbol, days.get(day)):\n                break",
     "            if False:\n                break"),
    ("lb_days_uses_ladder_level", CORE, '        row["lb_days"] = int(own)', "        pass"),
    ("provider_rank_swapped", CORE, "PROVIDER_RANK = {KAIPAN: 0, WENCAI: 1, TD_DERIVED: 2}",
     "PROVIDER_RANK = {KAIPAN: 2, WENCAI: 1, TD_DERIVED: 0}"),
    ("coverage_denominator_is_rows", CORE, "expected_count=seen, source_columns=columns,\n                   evidence=evidence, notes=notes,\n                   extra_quality={\"dropped_short_records\"",
     "expected_count=len(rows), source_columns=columns,\n                   evidence=evidence, notes=notes,\n                   extra_quality={\"dropped_short_records\""),
    ("kaipan_retry_until_ignored", RUNNER, "if exc.code == \"KAIPAN_NOT_READY\" and until and clock and clock >= until:",
     "if False:"),
    ("lower_quality_not_blocked", RUNNER, "if have is not None and have <= wanted:", "if False:"),
    ("locate_ignores_quality", STORAGE, "if dataset == \"limit_up_pool\" and len(versions) > 1:",
     "if False:"),
    ("td_readback_not_verified", "src/engine_core/data_pipeline/limit_pool_td.py",
     "        report = self.verify_day(trade_date, manifest, mapped)", "        report = {\"cells_compared\": 0}"),
]


def run_pytest(tree: Path) -> tuple[int, str]:
    env = {**os.environ, "PYTHONPATH": str(tree / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    done = subprocess.run([sys.executable, "-m", "pytest", "tests/test_limit_pool.py", "-q", "-x", "-p", "no:cacheprovider"],
                          cwd=tree, env=env, capture_output=True, text=True, timeout=300)
    tail = [line for line in done.stdout.splitlines() if line.startswith(("FAILED", "ERROR")) or " passed" in line or " failed" in line]
    return done.returncode, "; ".join(tail[-2:])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", default="")
    args = parser.parse_args(argv)
    wanted = {name.strip() for name in args.only.split(",") if name.strip()}
    work = Path(tempfile.mkdtemp(prefix="c580mut-"))
    survivors = 0
    try:
        base = work / "base"
        shutil.copytree(ROOT / "src", base / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (base / "tests").mkdir()
        for name in ("test_limit_pool.py", "conftest.py"):
            if (ROOT / "tests" / name).is_file():
                shutil.copy(ROOT / "tests" / name, base / "tests" / name)
        shutil.copytree(ROOT / "tests" / "fixtures" / "limit_pool", base / "tests" / "fixtures" / "limit_pool")
        (base / "native" / "guardiand" / "testdata").mkdir(parents=True)
        shutil.copy(ROOT / "native/guardiand/testdata/schedule.json", base / "native/guardiand/testdata/schedule.json")
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
            if name == "ladder_order_swapped":
                import json
                data = json.loads(text)
                event = next(e for e in data["events"] if e["event_id"] == "limit_pool_d1")
                data["profiles"][event["profile"]]["ladder"].reverse()
                path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            else:
                if text.count(old) != 1:
                    print(f"  ANCHOR  {name}: expected exactly one occurrence, found {text.count(old)}")
                    survivors += 1
                    continue
                path.write_text(text.replace(old, new), encoding="utf-8")
            code, summary = run_pytest(tree)
            killed = code != 0
            survivors += not killed
            print(f"  {'KILLED  ' if killed else 'SURVIVED'} {name}: {summary}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(f"survivors={survivors}")
    return 0 if survivors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
