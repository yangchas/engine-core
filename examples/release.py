#!/usr/bin/env python3
"""Release of merged Core code to the live bridge checkout - one evidence-gated, reversible step (C693).

  release.py plan                         what is merged but not live, what kind of change it is, and whether it may go
  release.py accept [--date D]            run EVERY scheduled data function once, sealed, for the candidate commit
  release.py apply --go --authorized-by   switch the live checkout to the candidate (Python code: no restart)
  release.py rollback --go --authorized-by

Why this exists: the 2026-11-05 audit found 18 merged cards that were not live and no way to say "this is ready". The
handlers run from the live checkout, one new process per attempt, so Python changes take effect at the next attempt and
need no restart; only schedule.json / timeline.json changes need a guardiand config swap (a separate, restart-window
step this tool does not do) and Go changes only matter if the binary is rebuilt (not done here).

``apply`` refuses unless: the candidate commit has a PASS acceptance record, the live checkout is clean and on the
commit this release started from, no handler is running (a running one imports lazily from the tree being switched),
and no event is due within the next two minutes. The user's go-word is recorded with the release.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples"))
LIVE = Path("/home/exedev/repos/engine_core-live")
EVIDENCE = Path("/home/exedev/fw/evidence/release")
BRANCH = "codex/feature-session-engine-integration"
QUIET_BEFORE, QUIET_AFTER = 30, 120        # seconds around "now" in which no event may be due
SMOKE_MODULES = ("engine_core.guardian.startup", "engine_core.data_pipeline.jobs", "engine_core.errors")


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if done.returncode:
        raise SystemExit(f"git {' '.join(args)} failed in {repo}: {done.stderr.strip()}")
    return done.stdout.strip()


def kind_of(path: str) -> str:
    if path.startswith(("tests/", "docs/", ".claude/")):
        return "tests_docs"
    if path.endswith(("src/engine_core/guardian/schedule.json", "src/engine_core/guardian/timeline.json")) \
            or path.startswith("native/guardiand/testdata/"):
        return "config"
    if path.startswith("native/"):
        return "go"
    return "python"


def classify(files: list[str]) -> dict[str, list[str]]:
    kinds: dict[str, list[str]] = {"python": [], "config": [], "go": [], "tests_docs": []}
    for name in files:
        kinds[kind_of(name)].append(name)
    return kinds


def running_handlers(live: Path, ps_lines: list[str] | None = None) -> list[str]:
    """Handler processes of the live checkout (gate and alert probes are short and do not import lazily)."""
    if ps_lines is None:
        ps_lines = subprocess.run(["ps", "-eo", "pid,args"], capture_output=True, text=True).stdout.splitlines()
    return [line.strip() for line in ps_lines if str(live) in line and "guardian_handler.py run" in line]


def events_due_soon(live: Path, now: datetime) -> list[str]:
    schedule = json.loads((live / "src" / "engine_core" / "guardian" / "schedule.json").read_text(encoding="utf-8"))
    seconds = now.hour * 3600 + now.minute * 60 + now.second
    due = []
    for event in schedule["events"]:
        text = event.get("at") or event.get("start")
        if isinstance(text, str) and text.count(":") >= 1:
            parts = [int(x) for x in text.split(":")] + [0, 0]
            at = parts[0] * 3600 + parts[1] * 60 + parts[2]
            if -QUIET_BEFORE <= at - seconds <= QUIET_AFTER:
                due.append(f"{event['event_id']} at {text}")
    return due


def acceptance(evidence_dir: Path, commit: str) -> dict[str, Any] | None:
    path = evidence_dir / f"accept-{commit}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def build_plan(live: Path, repo: Path, candidate: str, evidence_dir: Path, now: datetime,
               ps_lines: list[str] | None = None) -> dict[str, Any]:
    live_sha = git(live, "rev-parse", "HEAD")
    cand_sha = git(repo, "rev-parse", candidate)
    merges = git(repo, "log", "--first-parent", "--format=%h %s", f"{live_sha}..{cand_sha}").splitlines()
    files = [f for f in git(repo, "diff", "--name-only", f"{live_sha}..{cand_sha}").splitlines() if f]
    kinds = classify(files)
    record = acceptance(evidence_dir, cand_sha[:12])
    blockers = []
    if live_sha == cand_sha:
        blockers.append("live is already on the candidate")
    if git(live, "status", "--porcelain", "--untracked-files=no"):
        blockers.append("the live checkout has uncommitted changes")
    if not record or record.get("verdict") != "PASS":
        blockers.append("no PASS acceptance record for the candidate (run: release.py accept)")
    if running := running_handlers(live, ps_lines):
        blockers.append(f"{len(running)} handler(s) running from the live checkout")
    if due := events_due_soon(live, now):
        blockers.append("events due within two minutes: " + ", ".join(due[:4]))
    return {"live": live_sha[:12], "candidate": cand_sha[:12], "merges": merges, "files": {k: len(v) for k, v in kinds.items()},
            "config_files": kinds["config"], "go_files": kinds["go"],
            "restart_needed_for": ("the schedule/timeline change: swap guardiand config in a restart window "
                                   "(separate step)" if kinds["config"] else None),
            "acceptance": (record or {}).get("verdict"), "blockers": blockers, "ready": not blockers}


def cmd_plan(args: argparse.Namespace) -> int:
    plan = build_plan(args.live, ROOT, args.candidate, args.evidence_dir, datetime.now())
    print(f"live {plan['live']} -> candidate {plan['candidate']}: {len(plan['merges'])} merged change(s)")
    for line in plan["merges"]:
        print("  " + line)
    print("files:", ", ".join(f"{k}={v}" for k, v in plan["files"].items()))
    if plan["go_files"]:
        print("go source changed (the live binary is not rebuilt by this tool; fake-clock/test code only is expected)")
    if plan["restart_needed_for"]:
        print("config part: " + plan["restart_needed_for"] + " - " + ", ".join(plan["config_files"]))
    print("acceptance:", plan["acceptance"] or "none")
    for blocker in plan["blockers"]:
        print("BLOCKED:", blocker)
    print("READY" if plan["ready"] else "NOT READY")
    return 0 if plan["ready"] else 1


def judge(outcomes: list[dict[str, Any]], gaps: dict[str, list[str]]
          ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """(summary, answers outside the contract, answers nobody classified) of one run of every function."""
    summary, outside, unclassified = [], [], []
    for item in outcomes:
        reply = item["reply"] or {}
        text = json.dumps(reply, ensure_ascii=False)
        row = {"event": item["event"], "exit": item["exit"], "status": reply.get("status"),
               "error_code": reply.get("error_code") or None, "gaps": sorted(set(gaps.get(item["event"], [])))}
        summary.append(row)
        if item["problems"] or item["reply"] is None:
            outside.append({**row, "problems": item["problems"]})
        if reply.get("error_code") == "INTERNAL_ERROR" or '"unclassified": true' in text or '\\"unclassified\\":true' in text:
            unclassified.append(row)
    return summary, outside, unclassified


def cmd_accept(args: argparse.Namespace) -> int:
    import run_function
    commit = git(ROOT, "rev-parse", "--short=12", "HEAD")
    dirty = git(ROOT, "status", "--porcelain", "--untracked-files=no")
    if dirty:
        raise SystemExit("acceptance is recorded for a commit; commit or stash the changes first")
    schedule = json.loads(run_function.SCHEDULE.read_text(encoding="utf-8"))
    work = args.work_dir or Path(os.environ.get("TMPDIR", "/tmp")) / f"accept-{commit}"
    for sub in ("core-data", "replay", "home"):
        (work / sub).mkdir(parents=True, exist_ok=True)
    from replay_day import _seed_calendar
    dates = _seed_calendar(work / "core-data")
    outcomes = []
    for event in run_function.select(schedule, event=None, start="00:00:00", until="23:59:59", handler=None):
        outcomes.append(run_function.run_one(run_function.request_for(event, args.date, dates, schedule),
                                             event["handler"], work))
    summary, outside, unclassified = judge(outcomes, run_function.gaps_by_event(work / "replay"))
    verdict = "PASS" if not outside and not unclassified else "FAIL"
    record = {"commit": commit, "date": args.date, "at": datetime.now().isoformat(timespec="seconds"),
              "events": len(summary), "outside_contract": outside, "unclassified": unclassified, "verdict": verdict,
              "method": "examples/run_function.py: every scheduled function once, sealed (recorded Kaipan only; "
                        "Baostock/WenCai/Redis/TD are recorded gaps), reply checked against the contract",
              "outcomes": summary}
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    (args.evidence_dir / f"accept-{commit}.json").write_text(json.dumps(record, ensure_ascii=False, indent=1) + "\n",
                                                             encoding="utf-8")
    print(f"{commit}: {len(summary)} functions, {len(outside)} outside the contract, {len(unclassified)} unclassified"
          f" -> {verdict}")
    return 0 if verdict == "PASS" else 1


def _record(evidence_dir: Path, name: str, payload: dict[str, Any]) -> Path:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    path = evidence_dir / f"{name}-{datetime.now():%Y%m%dT%H%M%S}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path


def cmd_apply(args: argparse.Namespace) -> int:
    if not args.go or not args.authorized_by:
        raise SystemExit("apply changes the live checkout: pass --go and --authorized-by '<what the user said>'")
    plan = build_plan(args.live, ROOT, args.candidate, args.evidence_dir, datetime.now())
    if not plan["ready"]:
        for blocker in plan["blockers"]:
            print("BLOCKED:", blocker)
        return 1
    previous = git(args.live, "rev-parse", "HEAD")
    target = git(ROOT, "rev-parse", args.candidate)
    git(args.live, "checkout", "--detach", target)
    now = git(args.live, "rev-parse", "HEAD")
    if now != target:
        git(args.live, "checkout", "--detach", previous)
        raise SystemExit("the live checkout did not land on the candidate; restored the previous commit")
    smoke = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, 'src'); sys.path.insert(0, '.'); "
                            "import " + ", ".join(SMOKE_MODULES)], cwd=str(args.live), capture_output=True,
                           text=True) if SMOKE_MODULES else subprocess.CompletedProcess([], 0, "", "")
    if smoke.returncode:
        git(args.live, "checkout", "--detach", previous)
        raise SystemExit("import smoke failed, restored the previous commit:\n" + smoke.stderr[-600:])
    path = _record(args.evidence_dir, "deploy", {"previous": previous, "deployed": target, "merges": plan["merges"],
                                                 "authorized_by": args.authorized_by, "scope": "python (no restart)",
                                                 "config_not_applied": plan["config_files"],
                                                 "acceptance": plan["acceptance"]})
    print(f"live {previous[:12]} -> {target[:12]}; record {path}\nrollback: release.py rollback --go --authorized-by '...'")
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    if not args.go or not args.authorized_by:
        raise SystemExit("rollback changes the live checkout: pass --go and --authorized-by")
    records = sorted(args.evidence_dir.glob("deploy-*.json"))
    if not records:
        raise SystemExit("no deploy record to roll back")
    last = json.loads(records[-1].read_text(encoding="utf-8"))
    if git(args.live, "rev-parse", "HEAD") != last["deployed"]:
        raise SystemExit("the live checkout is not on the last deployed commit; nothing to roll back automatically")
    if handlers := running_handlers(args.live):
        raise SystemExit(f"{len(handlers)} handler(s) running from the live checkout")
    git(args.live, "checkout", "--detach", last["previous"])
    _record(args.evidence_dir, "rollback", {"from": last["deployed"], "to": last["previous"],
                                            "authorized_by": args.authorized_by})
    print(f"live back on {last['previous'][:12]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["plan", "accept", "apply", "rollback"])
    parser.add_argument("--live", type=Path, default=LIVE)
    parser.add_argument("--candidate", default=BRANCH)
    parser.add_argument("--evidence-dir", type=Path, default=EVIDENCE)
    parser.add_argument("--date", default="2026-11-05", help="accept: the trading day whose schedule is exercised")
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--go", action="store_true")
    parser.add_argument("--authorized-by", default="")
    args = parser.parse_args(argv)
    return {"plan": cmd_plan, "accept": cmd_accept, "apply": cmd_apply, "rollback": cmd_rollback}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
