#!/usr/bin/env python3
"""Fake-clock acceptance check for the ladder_daily trigger / backfill / upgrade / source-ladder chain (C577).

Drives the REAL guardian planning code (``reconcile_plan`` with the checked-in ``datasets.registry.json``) and the
REAL fetch-job argv from that registry (``examples/build_ladder_daily.py``) on a temporary catalog seeded with REAL
daily_kline and Kaipan limit_up_pool rows (tests/fixtures/ladder_daily). Only the clock is fake; no network (the live
Kaipan / WenCai rungs are replaced by recording doubles that are asserted on), no Redis, no ~/core-data.

  S0  17:40, daily_kline of the day absent             -> ladder NOT planned (no fixed-clock success)
  S1  daily_kline 10-26 PROVISIONAL lands               -> planned, built (derived_daily_kline), PROVISIONAL
  S2  reconcile again                                    -> quiescent
  S3  daily_kline 10-27 FORMAL lands, daemon starts late -> 10-27 backfilled: 002724 == 1, 600838 == 5
  S4  10-26 bars upgraded to FORMAL                      -> 10-26 rebuilt as NEW FORMAL version, 10-27 follows
  S5  stateless: a partition for 10-26 never existed (the C574 incident) -> 10-27 equals the sequential result
  S6  step by step: bars missing -> Kaipan pool -> WenCai -> UNKNOWN (partition refused / streak lower bound)
  S7  late arrival: the Kaipan pool shows up after an UNKNOWN day -> the partition is rebuilt with the new input

``--mutation`` breaks one piece and the run must FAIL:
  fixed_clock_trigger | no_input_refs | relay | no_continuity | no_fallback
Usage: python examples/guardian_ladder_trigger_check.py [--mutation NAME] [--keep]
"""
from __future__ import annotations

import argparse
import gzip
import importlib.util
import io
import json
import shutil
import sys
import tempfile
from contextlib import contextmanager, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from guardian_derived_trigger_check import seed  # noqa: E402

from engine_core import ladder_daily_adapter as adapter  # noqa: E402
from engine_core.data_pipeline import ladder_daily as ld  # noqa: E402
from engine_core.data_pipeline.storage import ArtifactCatalog  # noqa: E402
from engine_core.guardian import reconcile_plan as rp  # noqa: E402
from engine_core.guardian.registry import load_registry  # noqa: E402
from engine_core.guardian.startup import _verify_partition  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "ladder_daily" / "real_ladder_inputs.json.gz"
TZ = "+08:00"
CURRENT: dict = {}


def load_fixture() -> dict:
    with gzip.open(FIXTURE, "rt", encoding="utf-8") as handle:
        return json.load(handle)


class Doubles:
    """Replaces the live rungs. Every call is recorded; behaviour is set per scenario."""

    def __init__(self):
        self.kaipan_calls, self.wencai_calls = [], []
        self.kaipan_rows: dict[str, list] = {}
        self.wencai_rows: dict[str, tuple[list, str]] = {}

    def kaipan(self, day):
        self.kaipan_calls.append(day)
        rows = self.kaipan_rows.get(day)
        if rows is None:
            raise RuntimeError("kaipan_unreachable_double")
        return rows

    def wencai(self, day):
        self.wencai_calls.append(day)
        got = self.wencai_rows.get(day)
        if got is None:
            return {"status": "unavailable", "reason": "wencai_empty_double"}
        return {"status": "ok", "rows": [{"symbol": s} for s in got[0]], "date_evidence": got[1]}


class Run:
    def __init__(self, work: Path, fx: dict):
        self.root = work / "core-data"
        self.root.mkdir(parents=True)
        self.fx, self.days = fx, fx["days"]
        self.registry = {i["id"]: i for i in load_registry()}
        self.checks: list[dict] = []
        self.doubles = Doubles()
        calendar = {"days": [{"d": d, "t": 1} for d in self.days]}
        (self.root / "trade_calendar").mkdir()
        (self.root / "trade_calendar" / "calendar.json").write_text(json.dumps(calendar))
        CURRENT["root"] = self.root

    def check(self, name, ok, **detail):
        self.checks.append({"check": name, "ok": bool(ok), **detail})
        return bool(ok)

    def land_kline(self, day, status, source, at):
        k = self.fx["kline"][day]
        return seed(self.root, "daily_kline", day, k["rows"], source=source, status=status, available_at=at)

    def land_pool(self, day, at):
        p = self.fx["pools"][day]
        return seed(self.root, "limit_up_pool", day, p["rows"], source="kaipan_getHisBans", status=None, available_at=at)

    def catalog(self):
        return json.loads((self.root / "catalog.json").read_text())

    def versions(self, day):
        return self.catalog()["datasets"].get("ladder_daily", {}).get("dates", {}).get(day, [])

    def manifest(self, day, index=-1):
        return json.loads(Path(self.versions(day)[index]["manifest"]).read_text())

    def rows(self, day):
        return {r["symbol"]: r for r in ArtifactCatalog(self.root).read_rows(Path(self.versions(day)[-1]["manifest"]))}

    def plan(self, now, upto):
        cands = [d for d in self.days if d <= upto][-5:]
        full = rp.reconcile_plan(datetime.fromisoformat(now), self.catalog(), trade_date=upto, prev_trade_date=None,
                                 phase="POST_MARKET", load5=None, load_threshold=2.0, upgrade_attempts={},
                                 daily_kline_candidates=cands)
        return [p for p in full if p["id"] == "ladder_daily"]

    def run_job(self, entry, now):
        spec = importlib.util.spec_from_file_location("build_ladder_daily", ROOT / "examples" / "build_ladder_daily.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        item = self.registry["ladder_daily"]
        argv = [part.format(date=entry["partition"], python=sys.executable, artifact_root=str(self.root))
                for part in item["fetch_job"]["argv"]]
        assert argv[1].endswith("build_ladder_daily.py"), argv
        buffer = io.StringIO()
        with mock.patch.object(adapter, "default_kaipan_fetch", lambda max_requests=12: self.doubles.kaipan), \
                mock.patch.object(adapter, "default_wencai_fetch", lambda cfg, state: self.doubles.wencai), \
                redirect_stdout(buffer):
            rc = module.main(argv[2:] + ["--now", now, "--no-td"])
        if rc == 0:
            _verify_partition(item, entry["partition"], self.root, datetime.fromisoformat(now))
        return {"rc": rc, **json.loads(buffer.getvalue().strip().splitlines()[-1])}

    def execute(self, plan, now):
        return [{"partition": e["partition"], **self.run_job(e, now)} for e in plan if e.get("action") == "FETCH_NOW"]

    def settle(self, now, upto, rounds=8):
        out = []
        for i in range(rounds):
            results = self.execute(self.plan(now, upto), now)
            if not results:
                break
            out += results
        return out


def scenarios(run: Run) -> None:
    fx = run.fx
    early = [d for d in run.days if d < "2026-10-26"]
    for day in early:
        run.land_kline(day, "FORMAL", "baostock", f"{day}T18:30:00{TZ}")
        if day in fx["pools"]:
            nxt = run.days[run.days.index(day) + 1]
            run.land_pool(day, f"{nxt}T08:31:00{TZ}")
    # history: ladder for the days up to 10-23 exists (built by the same mechanism)
    run.settle(f"2026-10-24T09:00:00{TZ}", "2026-10-23")
    # S0 ---------------------------------------------------------------------------------------------------
    plan = run.plan(f"2026-10-26T17:40:00{TZ}", "2026-10-26")
    run.check("S0 no ladder planned at 17:40 while daily_kline of the day is absent",
              not [p for p in plan if p["partition"] == "2026-10-26"], plan=[(p["partition"], p.get("reason")) for p in plan])
    # S1 ---------------------------------------------------------------------------------------------------
    v26p = run.land_kline("2026-10-26", "PROVISIONAL", "legacy_q2_final", f"2026-10-26T21:02:30{TZ}")
    now = f"2026-10-26T21:05:00{TZ}"
    plan = run.plan(now, "2026-10-26")
    run.check("S1 ladder planned once the day's daily_kline (PROVISIONAL) landed",
              any(p["partition"] == "2026-10-26" and p["action"] == "FETCH_NOW" and p.get("reason") == "derive:missing"
                  for p in plan), plan=[(p["partition"], p["action"], p.get("reason")) for p in plan])
    results = run.execute(plan, now)
    run.check("S1 job exits 0", results and all(r["rc"] == 0 for r in results), rcs=[r["rc"] for r in results])
    m26 = run.manifest("2026-10-26")
    run.check("S1 built from the daily bars: source derived_daily_kline, PROVISIONAL, inputs record the bar version",
              m26["source"] == "derived_daily_kline" and m26["business_status"] == "PROVISIONAL"
              and any(r["dataset"] == "daily_kline" and r["trade_date"] == "2026-10-26" and r["version"] == v26p
                      for r in m26["input_refs"]))
    run.check("S1 no live rung was used (bars were continuous)", not run.doubles.kaipan_calls and not run.doubles.wencai_calls)
    # S2 ---------------------------------------------------------------------------------------------------
    plan = run.plan(f"2026-10-26T21:10:00{TZ}", "2026-10-26")
    run.check("S2 quiescent for up-to-date partitions", not plan, plan=[(p["partition"], p.get("reason")) for p in plan])
    # S3 ---------------------------------------------------------------------------------------------------
    run.land_kline("2026-10-27", "FORMAL", "baostock", f"2026-10-27T18:26:24{TZ}")
    now = f"2026-10-27T21:34:00{TZ}"
    plan = run.plan(now, "2026-10-27")
    run.check("S3 late start: 10-27 planned", any(p["partition"] == "2026-10-27" for p in plan),
              plan=[(p["partition"], p.get("reason")) for p in plan])
    run.execute(plan, now)
    r27 = run.rows("2026-10-27")
    run.check("S3 10-27: 002724 == 1 and 600838 == 5 (C574 incident)",
              r27["002724"]["streak_n"] == 1 and r27["600838"]["streak_n"] == 5,
              got=(r27["002724"]["streak_n"], r27["600838"]["streak_n"]))
    sequential_1027 = {s: r["streak_n"] for s, r in r27.items()}
    # S4 ---------------------------------------------------------------------------------------------------
    before26, before27 = len(run.versions("2026-10-26")), len(run.versions("2026-10-27"))
    run.land_kline("2026-10-26", "FORMAL", "baostock", f"2026-10-27T19:10:00{TZ}")
    now = f"2026-10-27T19:30:00{TZ}"
    plan = run.plan(now, "2026-10-27")
    run.check("S4 upgrade of the bars triggers a rebuild of the same partition (inputs_changed), oldest first",
              plan and plan[0]["partition"] == "2026-10-26" and plan[0].get("reason") == "derive:inputs_changed",
              plan=[(p["partition"], p.get("reason")) for p in plan])
    run.settle(now, "2026-10-27")
    run.check("S4 10-26 is a NEW FORMAL version, 10-27 followed",
              len(run.versions("2026-10-26")) == before26 + 1 and run.manifest("2026-10-26")["business_status"] == "FORMAL"
              and len(run.versions("2026-10-27")) == before27 + 1 and run.manifest("2026-10-27")["business_status"] == "FORMAL",
              statuses=(run.manifest("2026-10-26")["business_status"], run.manifest("2026-10-27")["business_status"]))
    run.check("S4 quiescent afterwards", not run.plan(f"2026-10-27T20:00:00{TZ}", "2026-10-27"))
    # S5 ---------------------------------------------------------------------------------------------------
    r2 = Run(Path(tempfile.mkdtemp(prefix="c577-s5-", dir=str(run.root.parent.parent))), fx)
    for day in early:
        r2.land_kline(day, "FORMAL", "baostock", f"{day}T18:30:00{TZ}")
    r2.land_kline("2026-10-26", "FORMAL", "baostock", f"2026-10-26T18:30:00{TZ}")
    r2.land_kline("2026-10-27", "FORMAL", "baostock", f"2026-10-27T18:26:24{TZ}")
    # ladder 10-23 exists, 10-26 NEVER built (the incident), 10-27 built directly
    r2.execute([{"partition": "2026-10-23", "action": "FETCH_NOW"}], f"2026-10-27T21:00:00{TZ}")
    r2.execute([{"partition": "2026-10-27", "action": "FETCH_NOW"}], f"2026-10-27T21:01:00{TZ}")
    got = {s: r["streak_n"] for s, r in r2.rows("2026-10-27").items()}
    run.check("S5 stateless: 10-27 built without a 10-26 partition equals the sequentially built one",
              not r2.versions("2026-10-26") and got == sequential_1027,
              diff=sorted(s for s in set(got) | set(sequential_1027) if got.get(s) != sequential_1027.get(s))[:5])
    shutil.rmtree(r2.root.parent, ignore_errors=True)
    # S6 ---------------------------------------------------------------------------------------------------
    # bars of 10-23 are missing: 10-23 itself has none, and 10-26 cannot be proven continuous with a missing
    # predecessor -> both days use another rung, 10-27 and the older days keep the bars.
    r3 = Run(Path(tempfile.mkdtemp(prefix="c577-s6-", dir=str(run.root.parent.parent))), fx)
    for day in [d for d in early if d != "2026-10-23"]:
        r3.land_kline(day, "FORMAL", "baostock", f"{day}T18:30:00{TZ}")
    r3.land_kline("2026-10-26", "FORMAL", "baostock", f"2026-10-26T18:30:00{TZ}")
    r3.land_kline("2026-10-27", "FORMAL", "baostock", f"2026-10-27T18:26:24{TZ}")
    wnow = f"2026-10-27T21:34:00{TZ}"
    cfg = {"kaipan": {"live": True}, "wencai": {"live": True}}

    def rebuild(minute):
        adapter.materialize_ladder_daily(ArtifactCatalog(r3.root), trade_date="2026-10-27", force=True,
                                         now=datetime.fromisoformat(wnow).replace(minute=minute),
                                         kaipan_fetch=r3.doubles.kaipan, wencai_fetch=r3.doubles.wencai, config=cfg)
        return r3.manifest("2026-10-27")

    pools = {d: [r["symbol"] for r in fx["pools"][d]["rows"]] for d in ("2026-10-23", "2026-10-26")}
    # step a: Kaipan pools are in the catalog (captured the morning after) -> those days use them
    r3.land_pool("2026-10-23", f"2026-10-26T08:31:00{TZ}")
    r3.land_pool("2026-10-26", f"2026-10-27T08:31:00{TZ}")
    r3.execute([{"partition": "2026-10-27", "action": "FETCH_NOW"}], wnow)
    m = r3.manifest("2026-10-27")
    src = {d: v["source"] for d, v in m["inputs"]["days"].items()}
    run.check("S6a bars of 10-23 missing -> 10-23 and 10-26 use the Kaipan pool, 10-27/10-22/10-21 keep the bars",
              src["2026-10-23"] == "kaipan" and src["2026-10-26"] == "kaipan" and src["2026-10-27"] == "derived_daily_kline"
              and src["2026-10-22"] == "derived_daily_kline", days=src)
    run.check("S6a chain intact (no truncation), no live call needed, 600838 still 5",
              not m["quality"]["partial"] and not r3.doubles.wencai_calls and not r3.doubles.kaipan_calls
              and r3.rows("2026-10-27")["600838"]["streak_n"] == 5)
    # step b: no pool in the catalog -> live Kaipan (historical day) answers
    shutil.rmtree(r3.root / "limit_up_pool")
    cat = r3.catalog()
    cat["datasets"].pop("limit_up_pool")
    (r3.root / "catalog.json").write_text(json.dumps(cat))
    for d in pools:
        r3.doubles.kaipan_rows[d] = [dict(r) for r in fx["pools"][d]["rows"]]
    m = rebuild(35)
    run.check("S6b no catalog pool -> live Kaipan answers those days, recorded in live_fetches",
              m["inputs"]["days"]["2026-10-26"]["source"] == "kaipan" and set(r3.doubles.kaipan_calls) >= set(pools)
              and {f["rung"] for f in m["live_fetches"]} == {"kaipan"})
    # step c: Kaipan unreachable, WenCai answers with date evidence
    r3.doubles.kaipan_rows.clear()
    for d in pools:
        r3.doubles.wencai_rows[d] = (pools[d], d)
    m = rebuild(36)
    run.check("S6c Kaipan unreachable -> WenCai (date evidence == day) answers",
              m["inputs"]["days"]["2026-10-26"]["source"] == "wencai" and "wencai" in m["provider_chain"],
              chain=m["provider_chain"])
    # step c2: WenCai answering another date is rejected
    r3.doubles.wencai_rows["2026-10-26"] = (pools["2026-10-26"], "2026-10-23")
    m = rebuild(37)
    run.check("S6c2 WenCai with the wrong date evidence is rejected -> that day UNKNOWN",
              m["inputs"]["days"]["2026-10-26"]["source"] == "UNKNOWN"
              and any("wencai_date_evidence_mismatch" in a["reason"] for a in m["provider_attempts"]))
    rows = r3.rows("2026-10-27")
    lb = [r for r in rows.values() if r["quality"].startswith("LOWER_BOUND")]
    run.check("S6d UNKNOWN day -> streaks through it are tagged lower bounds, partition DEGRADED/partial, no invented count",
              m["business_status"] == "DEGRADED" and m["quality"]["partial"] is True
              and m["quality"]["chain_truncated_days"] == ["2026-10-26"] and lb
              and all(r["streak_n"] == 1 for r in rows.values()), streak_600838=rows["600838"]["streak_n"], lb=len(lb))
    # step e: the target day itself unknown -> exit 20, nothing written
    n_before = len(r3.versions("2026-10-27"))
    r3.doubles.wencai_rows.clear()
    # S7: late arrival of the missing bars -> the guardian rebuilds (inputs changed)
    r3.land_kline("2026-10-23", "FORMAL", "baostock", f"2026-10-27T22:30:00{TZ}")
    r3.settle(f"2026-10-27T22:45:00{TZ}", "2026-10-26")      # older days still unbuilt in this temp root: build them
    plan = r3.plan(f"2026-10-27T23:00:00{TZ}", "2026-10-27")
    run.check("S7 late arrival of the missing bars -> partition planned for rebuild (inputs_changed)",
              any(p["partition"] == "2026-10-27" and p.get("reason") == "derive:inputs_changed" for p in plan),
              plan=[(p["partition"], p.get("reason")) for p in plan])
    r3.execute(plan, f"2026-10-27T23:00:00{TZ}")
    m = r3.manifest("2026-10-27")
    run.check("S7 rebuilt from the bars: chain intact, new version, rows equal the sequential result",
              m["inputs"]["days"]["2026-10-26"]["source"] == "derived_daily_kline" and not m["quality"]["partial"]
              and {s: r["streak_n"] for s, r in r3.rows("2026-10-27").items()} == sequential_1027
              and len(r3.versions("2026-10-27")) > n_before)
    shutil.rmtree(r3.root.parent, ignore_errors=True)


@contextmanager
def mutation(name):
    if name is None:
        yield
    elif name == "fixed_clock_trigger":
        def clock_only(catalog, dataset, day, now, min_status="PROVISIONAL"):
            absent = catalog.get("datasets", {}).get(dataset, {}).get("dates", {}).get(day) is None
            return "missing" if absent and (now.hour, now.minute) >= (17, 40) else None
        with mock.patch.object(rp, "derived_needs_build", clock_only):
            yield
    elif name == "no_input_refs":
        original = ld.build_ladder_daily

        def stripped(request):
            part = original(request)
            part.manifest["input_refs"] = []
            part.manifest["inputs"] = {}
            return part
        with mock.patch.object(adapter, "build_ladder_daily", stripped):
            yield
    elif name == "relay":
        # the pre-C577 algorithm: streak = (previous stored ladder streak or 0) + 1 when limit-up today
        original = ld.build_ladder_daily

        def relay(request):
            part = original(request)
            day = request["trade_date"]
            cat = ArtifactCatalog(CURRENT["root"])
            prior = sorted(d for d in cat.read().get("datasets", {}).get("ladder_daily", {}).get("dates", {}) if d < day)
            previous = {}
            if prior:
                previous = {r["symbol"]: r["streak_n"] for r in cat.read_rows(cat.locate("ladder_daily", prior[-1]))}
            for row in part.rows:
                row["streak_n"] = previous.get(row["symbol"], 0) + 1
            return part
        with mock.patch.object(adapter, "build_ladder_daily", relay):
            yield
    elif name == "no_continuity":
        with mock.patch.object(ld._Walk, "_continuous",
                               lambda self, d, part, before: (True, "", 1, 1)):
            yield
    elif name == "no_fallback":
        original = ld._Walk.__init__

        def init(self, request):
            original(self, request)
            self.ladder = [r for r in self.ladder if r == "daily_kline"]
        with mock.patch.object(ld._Walk, "__init__", init):
            yield
    else:
        raise SystemExit(f"unknown mutation {name}")


def run_all(mutation_name=None, keep=False):
    work = Path(tempfile.mkdtemp(prefix="c577-trigger-"))
    run = Run(work, load_fixture())
    try:
        with mutation(mutation_name):
            try:
                scenarios(run)
            except Exception as exc:  # a broken mechanism may raise instead of failing a check
                import traceback
                run.check(f"scenario aborted: {type(exc).__name__}", False, error=str(exc)[:200],
                          tb=traceback.format_exc()[-300:])
        return run.checks
    finally:
        if not keep:
            shutil.rmtree(work, ignore_errors=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mutation", choices=("fixed_clock_trigger", "no_input_refs", "relay", "no_continuity",
                                               "no_fallback"))
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args(argv)
    checks = run_all(args.mutation, args.keep)
    failed = [c for c in checks if not c["ok"]]
    for c in checks:
        print(("PASS " if c["ok"] else "FAIL ") + c["check"] + ("" if c["ok"] else "  " + json.dumps(
            {k: v for k, v in c.items() if k not in ("check", "ok")}, default=str)[:300]))
    print(f"{len(checks) - len(failed)}/{len(checks)} checks passed" + (f" (mutation={args.mutation})" if args.mutation else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
