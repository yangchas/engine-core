#!/usr/bin/env python3
"""Print the timeline view of a schedule (design 28, P2.1) and check it expands back to the same schedule.

  schedule_to_timeline.py [--schedule PATH] [--out PATH]
Read-only unless --out is given; never touches the live configuration.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from engine_core.guardian.timeline import diff_schedules, expand, generate, to_timeline  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schedule", default=str(ROOT / "src" / "engine_core" / "guardian" / "schedule.json"))
    parser.add_argument("--out")
    parser.add_argument("--generate", help="write the canonical schedule text generated from the timeline")
    args = parser.parse_args(argv)
    schedule = json.loads(Path(args.schedule).read_text(encoding="utf-8"))
    doc = to_timeline(schedule)
    canon = lambda value: json.dumps(value, sort_keys=True, ensure_ascii=False)  # noqa: E731
    lossless = canon(expand(doc)) == canon(schedule)
    report = {"events": len(schedule["events"]), "functions": len(doc["functions"]),
              "time_points": len(doc["timeline"]), "startup_events": len(doc["startup"]),
              "policies": len(doc["policies"]), "bytes_flat": len(canon(schedule)),
              "bytes_timeline": len(canon(doc)), "lossless": lossless}
    print(json.dumps(report, ensure_ascii=False))
    if args.out:
        Path(args.out).write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if args.generate:
        text = generate(doc)
        Path(args.generate).write_text(text, encoding="utf-8")
        differences = diff_schedules(schedule, json.loads(text))
        print(json.dumps({"generated_bytes": len(text.encode()), "differences_from_input": differences,
                          "byte_identical_to_input": text == Path(args.schedule).read_text(encoding="utf-8")}))
        lossless = lossless and not differences
    return 0 if lossless else 1


if __name__ == "__main__":
    raise SystemExit(main())
