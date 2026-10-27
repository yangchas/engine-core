#!/usr/bin/env python3
"""Fake-clock acceptance check for the derived daily datasets' trigger/backfill/upgrade chain (C571).

Drives the REAL guardian planning code (``reconcile_plan`` with the checked-in ``datasets.registry.json``) and the
REAL fetch-job argv from that registry (``examples/build_daily_derived.py``), on a temporary catalog seeded with
real daily_kline rows (tests/fixtures/derived_daily, see PROVENANCE.md). Only the clock is fake: upstream
partitions "arrive" at declared fake instants and reconcile runs at fake instants. No network, no Redis, no
~/core-data, no guardiand.

Scenarios (each asserts):
  S0  upstream absent at the old fixed trigger time (17:40)  -> derived NOT planned (no clock-only success)
  S1  daily_kline 10-26 PROVISIONAL lands                    -> derived planned, built as PROVISIONAL, versions recorded
  S2  reconcile again                                         -> nothing to do (idempotent, no new version)
  S3  daily_kline 10-27 FORMAL lands, daemon started late     -> derived 10-27 backfilled as FORMAL
  S4  daily_kline 10-26 upgraded to FORMAL                    -> 10-26 recomputed FORMAL (new version), 10-27 follows
  S5  cross-partition: load-deferred daily_kline upgrade of another day does not block derived (10-27 incident)

``--mutation`` breaks one piece and the run must FAIL: no_input_refs | fixed_clock_trigger | bad_window.
Usage: python examples/guardian_derived_trigger_check.py [--mutation NAME] [--keep]
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.util
import json
import shutil
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from engine_core.data_pipeline import derived_daily  # noqa: E402
from engine_core.data_pipeline.storage import ArtifactCatalog, canonical_json  # noqa: E402
from engine_core.guardian import reconcile_plan as rp  # noqa: E402
from engine_core.guardian.registry import load_registry  # noqa: E402
from engine_core.guardian.startup import _verify_partition  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "derived_daily" / "real_kline_history.json.gz"
TZ = "+08:00"
DERIVE_IDS = ("daily_indicators", "daily_report_features")


def load_fixture() -> dict[str, Any]:
    with gzip.open(FIXTURE, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def seed(root: Path, dataset: str, day: str, rows: list[dict], *, source: str, status: str | None,
         available_at: str, quality: dict | None = None) -> str:
    """Write one upstream partition and register it in catalog.json exactly like ArtifactCatalog.commit would."""
    payload = b"\n".join(canonical_json(r) for r in rows) + b"\n"
    digest = hashlib.sha256(payload + canonical_json([source, status, available_at])).hexdigest()
    version = f"sha256-{digest[:16]}"
    run_dir = root / dataset / f"trade_date={day}" / f"source={source}" / version
    manifest = {"schema_version": 1, "dataset": dataset, "trade_date": day, "source": source, "provider": source,
                "business_status": status, "available_at": available_at, "row_count": len(rows),
                "version": version, "sha256": digest, "rows_uri": str(run_dir / "rows.jsonl.gz"),
                "manifest_uri": str(run_dir / "manifest.json"), "quality": quality or {"coverage": 1.0, "status": "READY"}}
    _write(run_dir / "rows.jsonl.gz", gzip.compress(payload, mtime=0))
    _write(run_dir / "manifest.json", canonical_json(manifest) + b"\n")
    path = root / "catalog.json"
    catalog = json.loads(path.read_text()) if path.is_file() else {"schema_version": 1, "datasets": {}}
    entry = catalog["datasets"].setdefault(dataset, {"dates": {}, "latest": None})
    entry["dates"].setdefault(day, []).append({
        "version": version, "manifest": str(run_dir / "manifest.json"), "sha256": digest, "row_count": len(rows),
        "as_of": day, "source": source, "business_status": status, "available_at": available_at})
    if status != "PROVISIONAL" and (entry["latest"] is None or day >= entry["latest"]["trade_date"]):
        entry["latest"] = {"trade_date": day, "version": version}
    path.write_bytes(canonical_json(catalog) + b"\n")
    return version


class Run:
    def __init__(self, work: Path, fixture: dict[str, Any]):
        self.root = work / "core-data"
        self.root.mkdir(parents=True)
        self.fx = fixture
        self.days = [p["trade_date"] for p in fixture["kline"]]
        self.rows = {p["trade_date"]: p["rows"] for p in fixture["kline"]}
        self.checks: list[dict[str, Any]] = []
        self.registry = {i["id"]: i for i in load_registry()}

    def check(self, name: str, ok: bool, **detail: Any) -> bool:
        self.checks.append({"check": name, "ok": bool(ok), **detail})
        return bool(ok)

    # ------------------------------------------------------------------ upstream arrival
    def land_universe(self, day: str) -> None:
        seed(self.root, "symbol_universe", day, self.fx["universe"][day]["rows"], source="baostock_query_all_stock",
             status=None, available_at=f"{day}T08:45:00{TZ}")

    def land_kline(self, day: str, status: str, source: str, at: str) -> str:
        return seed(self.root, "daily_kline", day, self.rows[day], source=source, status=status, available_at=at,
                    quality={"coverage": 1.0, "status": "READY", "partial": False})

    def catalog(self) -> dict:
        return json.loads((self.root / "catalog.json").read_text())

    def candidates(self, upto: str) -> list[str]:
        return [d for d in self.days if d <= upto][-5:]

    # ------------------------------------------------------------------ guardian planning + job execution
    def plan(self, now: str, upto: str, *, load5: float | None = None) -> list[dict]:
        full = rp.reconcile_plan(datetime.fromisoformat(now), self.catalog(), trade_date=upto,
                                 prev_trade_date=None, phase="POST_MARKET", load5=load5, load_threshold=2.0,
                                 upgrade_attempts={}, daily_kline_candidates=self.candidates(upto))
        return [p for p in full if p["id"] in DERIVE_IDS or p["id"] == "daily_kline"]

    def run_job(self, entry: dict, now: str) -> dict:
        """Execute the registry's fetch_job argv for a plan entry in-process (fake clock), then verify like guardian."""
        spec = importlib.util.spec_from_file_location("build_daily_derived", ROOT / "examples" / "build_daily_derived.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        item = self.registry[entry["id"]]
        argv = [part.format(date=entry["partition"], python=sys.executable, artifact_root=str(self.root))
                for part in item["fetch_job"]["argv"]]
        assert argv[1].endswith("build_daily_derived.py"), argv
        import io
        from contextlib import redirect_stdout
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            rc = module.main(argv[2:] + ["--now", now])
        _verify_partition(item, entry["partition"], self.root, datetime.fromisoformat(now))
        return {"rc": rc, **json.loads(buffer.getvalue().strip().splitlines()[-1])}

    def execute(self, plan: list[dict], now: str) -> list[dict]:
        results = []
        for entry in plan:
            if entry["id"] in DERIVE_IDS and entry.get("action") == "FETCH_NOW":
                results.append({"id": entry["id"], "partition": entry["partition"], **self.run_job(entry, now)})
        return results

    # ------------------------------------------------------------------ catalog readers
    def versions(self, dataset: str, day: str) -> list[dict]:
        return self.catalog()["datasets"].get(dataset, {}).get("dates", {}).get(day, [])

    def manifest(self, dataset: str, day: str, index: int = -1) -> dict:
        return json.loads(Path(self.versions(dataset, day)[index]["manifest"]).read_text())

    def derived_rows(self, dataset: str, day: str) -> list[dict]:
        return ArtifactCatalog(self.root).read_rows(Path(self.versions(dataset, day)[-1]["manifest"]))


def independent_indicators(history_rows: dict[str, list[dict]], day: str, symbol: str) -> dict[str, float | None]:
    """Plain-python re-computation of ma5/ma10/ma20/bias_20/change_pct_5d from raw close prices (no daily_history)."""
    closes = [r["close"] for d in sorted(history_rows) if d <= day
              for r in history_rows[d] if r["symbol"] == symbol and r.get("close") is not None]
    out: dict[str, float | None] = {}
    for n in (5, 10, 20):
        out[f"ma{n}"] = round(sum(closes[-n:]) / n, 2) if len(closes) >= n else None
    out["bias_20"] = round((closes[-1] - out["ma20"]) / out["ma20"] * 100, 2) if out["ma20"] else None
    out["change_pct_5d"] = round((closes[-1] / closes[-6] - 1) * 100, 2) if len(closes) >= 6 else None
    return out


def verify_indicator_values(run: Run, day: str, label: str) -> None:
    rows = {r["symbol"]: r for r in run.derived_rows("daily_indicators", day)}
    bad = []
    for symbol in sorted(rows)[:40]:
        expected = independent_indicators(run.rows, day, symbol)
        for column, value in expected.items():
            got = rows[symbol].get(column)
            if (value is None) != (got is None) or (value is not None and abs(value - got) > 0.011):
                bad.append((symbol, column, value, got))
    run.check(f"{label}: indicators equal independent recomputation (40 symbols)", not bad, mismatches=bad[:3])


def scenarios(run: Run) -> None:
    early = [d for d in run.days if d < "2026-10-26"]
    for day in run.days:
        if run.fx["universe"].get(day):
            run.land_universe(day)
    for day in early:
        run.land_kline(day, "FORMAL", "baostock", f"{day}T18:30:00{TZ}")
    # S0 -------------------------------------------------------------------------------------------------
    plan = run.plan(f"2026-10-26T17:40:00{TZ}", "2026-10-26")
    run.check("S0 no derived planned at 17:40 while daily_kline of the day is absent",
              not [p for p in plan if p["id"] in DERIVE_IDS], plan=[p["id"] for p in plan])
    # S1 -------------------------------------------------------------------------------------------------
    v26p = run.land_kline("2026-10-26", "PROVISIONAL", "legacy_q2_final", f"2026-10-26T21:02:30{TZ}")
    now = f"2026-10-26T21:05:00{TZ}"
    plan = run.plan(now, "2026-10-26")
    derive = [p for p in plan if p["id"] in DERIVE_IDS]
    run.check("S1 derived planned once upstream PROVISIONAL landed, indicators before features",
              [p["id"] for p in derive] == ["daily_indicators", "daily_report_features"]
              and all(p["action"] == "FETCH_NOW" and p["partition"] == "2026-10-26" for p in derive),
              plan=[(p["id"], p["action"], p.get("partition")) for p in derive])
    results = run.execute(plan, now)
    run.check("S1 jobs exit 0", all(r["rc"] == 0 for r in results) and len(results) == 2, results=[r["rc"] for r in results])
    mi, mf = run.manifest("daily_indicators", "2026-10-26"), run.manifest("daily_report_features", "2026-10-26")
    run.check("S1 products are PROVISIONAL with source derived_v1 and empty provider_chain",
              mi["business_status"] == mf["business_status"] == "PROVISIONAL" and mi["source"] == "derived_v1"
              and mi["provider_chain"] == [] and mf["provider_chain"] == [])
    run.check("S1 manifest records the exact upstream versions",
              mi.get("inputs", {}).get("daily_kline", {}).get("version") == v26p
              and {"dataset": "daily_kline", "trade_date": "2026-10-26", "version": v26p,
                   "business_status": "PROVISIONAL"} in mi.get("input_refs", [])
              and mf.get("inputs", {}).get("daily_indicators", {}).get("version")
              == run.versions("daily_indicators", "2026-10-26")[-1]["version"])
    verify_indicator_values(run, "2026-10-26", "S1")
    # S2 -------------------------------------------------------------------------------------------------
    plan = run.plan(f"2026-10-26T21:10:00{TZ}", "2026-10-26")
    run.check("S2 nothing planned for up-to-date derived partitions", not [p for p in plan if p["id"] in DERIVE_IDS],
              plan=[p["id"] for p in plan])
    # S3 -------------------------------------------------------------------------------------------------
    v27 = run.land_kline("2026-10-27", "FORMAL", "baostock", f"2026-10-27T18:26:24{TZ}")
    now = f"2026-10-27T21:34:00{TZ}"   # daemon (re)started long after the upstream landed
    plan = run.plan(now, "2026-10-27")
    derive = [p for p in plan if p["id"] in DERIVE_IDS]
    run.check("S3 late start backfills 10-27 derived (not only the fixed-time slot)",
              [p["id"] for p in derive] == ["daily_indicators", "daily_report_features"]
              and all(p["partition"] == "2026-10-27" for p in derive), plan=[(p["id"], p.get("partition")) for p in derive])
    run.execute(plan, now)
    m27 = run.manifest("daily_indicators", "2026-10-27")
    run.check("S3 10-27 derived is FORMAL (own-day upstream FORMAL; PROVISIONAL history is recorded not inherited)",
              m27["business_status"] == "FORMAL" and m27.get("inputs", {}).get("daily_kline", {}).get("version") == v27
              and m27["quality"]["history_non_formal_days"] == ["2026-10-26"], q=m27["quality"].get("history_non_formal_days"))
    # S4 -------------------------------------------------------------------------------------------------
    before = {d: [v["version"] for v in run.versions("daily_indicators", d)] for d in ("2026-10-26", "2026-10-27")}
    rows_before = {d: hashlib.sha256(json.dumps(run.derived_rows("daily_indicators", d), sort_keys=True).encode()).hexdigest()
                   for d in before}
    v26f = run.land_kline("2026-10-26", "FORMAL", "baostock", f"2026-10-27T19:10:00{TZ}")
    now = f"2026-10-27T19:30:00{TZ}"
    plan = run.plan(now, "2026-10-27")
    derive = [p for p in plan if p["id"] in DERIVE_IDS]
    run.check("S4 upgrade of daily_kline triggers recompute of the SAME partition first (oldest stale)",
              derive and derive[0]["id"] == "daily_indicators" and derive[0]["partition"] == "2026-10-26"
              and derive[0]["reason"] == "derive:inputs_changed", plan=[(p["id"], p.get("partition"), p.get("reason")) for p in derive])
    for _ in range(4):   # reconcile rounds until quiescent (bounded)
        results = run.execute(plan, now)
        if not results:
            break
        now = f"{now[:11]}{int(now[11:13]):02d}:{min(int(now[14:16]) + 5, 59):02d}:00{TZ}"
        plan = run.plan(now, "2026-10-27")
    a26, a27 = run.versions("daily_indicators", "2026-10-26"), run.versions("daily_indicators", "2026-10-27")
    m26 = run.manifest("daily_indicators", "2026-10-26")
    run.check("S4 10-26 recomputed as a NEW FORMAL version recording the FORMAL daily_kline",
              len(a26) == len(before["2026-10-26"]) + 1 and m26["business_status"] == "FORMAL"
              and m26.get("inputs", {}).get("daily_kline", {}).get("version") == v26f, versions=len(a26))
    run.check("S4 same rows, new version (input identity is part of the version)",
              m26["version"] != before["2026-10-26"][-1]
              and hashlib.sha256(json.dumps(run.derived_rows("daily_indicators", "2026-10-26"), sort_keys=True).encode()).hexdigest()
              == rows_before["2026-10-26"])
    run.check("S4 10-27 follows (its recorded history version of 10-26 changed)",
              len(a27) == len(before["2026-10-27"]) + 1 and a27[-1]["version"] != before["2026-10-27"][-1],
              versions=len(a27))
    f26 = run.manifest("daily_report_features", "2026-10-26")
    run.check("S4 report features recomputed FORMAL against the new indicators version",
              f26["business_status"] == "FORMAL"
              and f26.get("inputs", {}).get("daily_indicators", {}).get("version") == a26[-1]["version"])
    plan = run.plan(f"2026-10-27T20:00:00{TZ}", "2026-10-27")
    run.check("S4 quiescent afterwards", not [p for p in plan if p["id"] in DERIVE_IDS], plan=[p["id"] for p in plan])
    best = run.catalog()["datasets"]["daily_indicators"]["dates"]["2026-10-26"]
    run.check("S4 old PROVISIONAL version is retained, newest is FORMAL",
              [v["business_status"] for v in best] == ["PROVISIONAL", "FORMAL"], statuses=[v["business_status"] for v in best])
    # S5 -------------------------------------------------------------------------------------------------
    r2 = Run(Path(tempfile.mkdtemp(prefix="c571-s5-", dir=str(run.root.parent.parent))), run.fx)
    for day in r2.days:
        if r2.fx["universe"].get(day):
            r2.land_universe(day)
    for day in [d for d in r2.days if d < "2026-10-26"]:
        r2.land_kline(day, "FORMAL", "baostock", f"{day}T18:30:00{TZ}")
    r2.land_kline("2026-10-22", "PROVISIONAL", "wencai", "2026-10-22T21:00:00+08:00")   # an old partition awaiting upgrade
    r2.land_kline("2026-10-26", "FORMAL", "baostock", f"2026-10-26T18:30:00{TZ}")
    r2.land_kline("2026-10-27", "FORMAL", "baostock", f"2026-10-27T18:26:24{TZ}")
    plan = r2.plan(f"2026-10-27T21:34:00{TZ}", "2026-10-27", load5=2.94)
    by = {(p["id"], p.get("partition")): p for p in plan}
    kl = [p for p in plan if p["id"] == "daily_kline"]
    ind = [p for p in plan if p["id"] == "daily_indicators"]
    run.check("S5 daily_kline upgrade of another day is load-deferred ...", kl and kl[0]["action"] == "DEFER", plan=[(p["id"], p["action"]) for p in plan])
    run.check("S5 ... but derived partitions are not blocked by it",
              ind and ind[0]["action"] == "FETCH_NOW" and "blocked_by" not in str(ind[0].get("reason")), ind=ind[:1])
    shutil.rmtree(r2.root.parent, ignore_errors=True)


@contextmanager
def mutation(name: str | None):
    if name is None:
        yield
    elif name == "no_input_refs":
        # drop the upstream version record: inputs empty -> FORMAL upgrade is invisible and the version never changes
        with mock.patch.object(derived_daily, "_refs", lambda ctx, extra=(): []), \
                mock.patch.object(derived_daily, "_kline_input", lambda ctx: {}):
            yield
    elif name == "fixed_clock_trigger":
        def clock_only(catalog, dataset, day, now, min_status="PROVISIONAL"):
            absent = (catalog.get("datasets", {}).get(dataset, {}).get("dates", {}).get(day) is None)
            return "missing" if absent and (now.hour, now.minute) >= (17, 40) else None
        with mock.patch.object(rp, "derived_needs_build", clock_only):
            yield
    elif name == "bad_window":
        # the rolling window silently shrinks: ma20/vol_ratio windows lose their history
        original = derived_daily._prepare
        def shrunk(request):
            return original({**request, "window": {"max_days": 20}, "daily_kline": request["daily_kline"][-19:]
                             + [p for p in request["daily_kline"] if p["trade_date"] == request["trade_date"]][-1:]})
        with mock.patch.object(derived_daily, "_prepare", shrunk):
            yield
    else:
        raise SystemExit(f"unknown mutation {name}")


def run_all(mutation_name: str | None = None, keep: bool = False) -> list[dict[str, Any]]:
    work = Path(tempfile.mkdtemp(prefix="c571-trigger-"))
    run = Run(work, load_fixture())
    try:
        with mutation(mutation_name):
            try:
                scenarios(run)
            except Exception as exc:  # a broken mechanism may raise instead of failing a check
                run.check(f"scenario aborted: {type(exc).__name__}", False, error=str(exc)[:200])
        return run.checks
    finally:
        if not keep:
            shutil.rmtree(work, ignore_errors=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mutation", choices=("no_input_refs", "fixed_clock_trigger", "bad_window"))
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args(argv)
    checks = run_all(args.mutation, args.keep)
    failed = [c for c in checks if not c["ok"]]
    for c in checks:
        print(("PASS " if c["ok"] else "FAIL ") + c["check"] + ("" if c["ok"] else "  " + json.dumps({k: v for k, v in c.items() if k not in ("check", "ok")}, default=str)[:300]))
    print(f"{len(checks) - len(failed)}/{len(checks)} checks passed" + (f" (mutation={args.mutation})" if args.mutation else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
