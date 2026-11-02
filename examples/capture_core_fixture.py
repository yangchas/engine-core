#!/usr/bin/env python3
"""Copy REAL Core artifacts (catalog snapshot + best manifest per dataset/day) into tests/fixtures/real/core_artifacts.

Fixtures are cp-copies of what the live system wrote, registered in tests/fixtures/real/PROVENANCE.json
(`_core_artifacts`: source path, sha256, copy command, copied_at). Nothing is edited or hand-written (docs/TESTING.md).

    python examples/capture_core_fixture.py --card c639 --from 2026-10-19 --to 2026-10-30 \
        --datasets daily_indicators daily_report_features ladder_daily stock_day_facts
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from engine_core.data_pipeline.derived_daily import select_best  # noqa: E402

REAL = ROOT / "tests" / "fixtures" / "real"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--card", required=True)
    parser.add_argument("--core-root", default="/home/exedev/core-data")
    parser.add_argument("--from", dest="start", required=True)
    parser.add_argument("--to", dest="end", required=True)
    parser.add_argument("--datasets", nargs="+", required=True)
    args = parser.parse_args(argv)
    core = Path(args.core_root)
    provenance_path = REAL / "PROVENANCE.json"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    index = provenance.setdefault("_core_artifacts", {})
    now = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
    catalog_file = core / "catalog.json"
    catalog = json.loads(catalog_file.read_text(encoding="utf-8"))
    copies = [(catalog_file, f"core_artifacts/{args.card}/catalog.json", "core_catalog_snapshot", None, None, None)]
    for dataset in args.datasets:
        for day, entries in sorted(catalog["datasets"].get(dataset, {}).get("dates", {}).items()):
            if not args.start <= day <= args.end:
                continue
            best = select_best([e for e in entries if isinstance(e, dict)])
            if not best or not best.get("manifest"):
                continue
            source = Path(best["manifest"])
            relative = source.relative_to(core)
            copies.append((source, f"core_artifacts/{args.card}/{relative.as_posix()}", "core_artifact_manifest",
                           dataset, day, best.get("version")))
    for source, name, kind, dataset, day, version in copies:
        target = REAL / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        body = target.read_bytes()
        index[name] = {"bytes": len(body), "command": f"cp {source} tests/fixtures/real/{name}", "copied_at": now,
                       "dataset": dataset or "catalog", "kind": kind, "sha256": hashlib.sha256(body).hexdigest(),
                       "source": "core_data", "source_path": str(source), "trade_date": day, "version": version}
    provenance_path.write_text(json.dumps(provenance, indent=1, ensure_ascii=False) + "\n",
                               encoding="utf-8")
    print(f"captured {len(copies)} files under tests/fixtures/real/core_artifacts/{args.card}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
