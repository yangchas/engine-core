#!/usr/bin/env python3
"""Generate schedule.json from the hand-edited timeline.json (design 28, P2.4).

  generate_schedule.py            write schedule.json, the native testdata copy and its provenance sha
  generate_schedule.py --check    change nothing; exit 1 when any of those three differs from what timeline.json yields

timeline.json is the only file to edit by hand. The generated schedule.json is still committed: guardiand's migrator,
make_config.py and the tests read it unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from engine_core.guardian.timeline import generate  # noqa: E402
from engine_core.guardian.timeline_check import validate_timeline  # noqa: E402



def paths(root: Path) -> tuple[Path, Path, Path, Path]:
    guardian = root / "src" / "engine_core" / "guardian"
    testdata = root / "native" / "guardiand" / "testdata"
    return guardian / "timeline.json", guardian / "schedule.json", testdata / "schedule.json", testdata / "provenance.json"


def desired(root: Path = ROOT) -> tuple[str, str]:
    """(schedule text, provenance text) for the current timeline.json; raises on validation problems."""
    timeline, _schedule, _native, provenance_path = paths(root)
    doc = json.loads(timeline.read_text(encoding="utf-8"))
    problems = validate_timeline(doc)
    if problems:
        raise SystemExit("timeline.json is invalid:\n  " + "\n  ".join(problems))
    text = generate(doc)
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    provenance["sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return text, json.dumps(provenance, indent=2) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--root", default=str(ROOT), help="repository root (tests use a temporary copy)")
    args = parser.parse_args(argv)
    root = Path(args.root)
    text, provenance = desired(root)
    _timeline, schedule, native, provenance_path = paths(root)
    targets = [(schedule, text), (native, text), (provenance_path, provenance)]
    stale = [str(path.relative_to(root)) for path, body in targets
             if not path.exists() or path.read_text(encoding="utf-8") != body]
    if args.check:
        print(json.dumps({"stale": stale}))
        return 1 if stale else 0
    for path, body in targets:
        path.write_text(body, encoding="utf-8")
    print(json.dumps({"written": stale}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
