"""Tests for the dataset contract checker (C570). No network.

Positive/negative cases run on a conformant synthetic partition written by an independent
writer (not ArtifactCatalog.commit, whose daily_kline preparation drops contract fields).
The mutation tests additionally run on a copy of a real core-data partition when present.
"""
from __future__ import annotations

import copy
import gzip
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from engine_core.data_pipeline.dataset_contract import check_partition, load_spec
from engine_core.data_pipeline.storage import canonical_json

REPO = Path(__file__).resolve().parents[1]
DATE = "2026-10-27"
REAL_ROOT = Path.home() / "core-data"


def _payload(rows):
    return b"\n".join(canonical_json(r) for r in rows) + (b"\n" if rows else b"")


def write_partition(root: Path, dataset: str, rows, manifest_extra=None, *, source="baostock",
                    status="FORMAL", date=DATE, make_latest=True, gz_payload=None):
    payload = _payload(rows)
    digest = hashlib.sha256(payload).hexdigest()
    version = f"sha256-{digest[:16]}"
    run = root / dataset / f"trade_date={date}" / f"source={source}" / version
    run.mkdir(parents=True, exist_ok=True)
    (run / "rows.jsonl.gz").write_bytes(gzip.compress(gz_payload if gz_payload is not None else payload, mtime=0))
    manifest = {
        "schema_version": 1, "dataset": dataset, "trade_date": date, "source": source, "provider": source,
        "provider_chain": [source],
        "provider_attempts": [{"provider": source, "result": "ACCEPTED", "elapsed_ms": 10, "reason": "ok",
                               "row_count": len(rows)}],
        "available_at": f"{date}T18:20:00+08:00", "fetched_at": f"{date}T18:10:00+08:00",
        "as_of": f"{date}T00:00:00+08:00", "business_status": status, "degraded": status == "DEGRADED",
        "row_count": len(rows), "version": version, "sha256": digest,
        "quality": {"coverage": 1.0, "expected_basis": "universe_active", "expected_count": None, "missing": 0,
                    "field_completeness": {}, "partial": False, "dropped_outside_universe": 0,
                    "dropped_duplicates": 0},
        "field_units": {"open": "yuan", "high": "yuan", "low": "yuan", "close": "yuan", "pre_close": "yuan",
                        "pct_chg": "percent", "volume": "shares", "amount_yuan": "yuan",
                        "turnover": "percent", "turnover_pct": "percent"},
        "rows_uri": str(run / "rows.jsonl.gz"), "manifest_uri": str(run / "manifest.json"),
    }
    manifest.update(manifest_extra or {})
    (run / "manifest.json").write_bytes(canonical_json(manifest) + b"\n")
    cat_path = root / "catalog.json"
    cat = json.loads(cat_path.read_text()) if cat_path.exists() else {"schema_version": 1, "datasets": {}}
    entry = cat["datasets"].setdefault(dataset, {"dates": {}, "latest": None})
    entry["dates"].setdefault(date, []).append({
        "version": version, "manifest": str(run / "manifest.json"), "sha256": digest, "row_count": len(rows),
        "as_of": manifest["as_of"], "source": source, "business_status": status,
        "available_at": manifest["available_at"]})
    if make_latest and status != "PROVISIONAL":
        entry["latest"] = {"trade_date": date, "version": version}
        (root / dataset).mkdir(parents=True, exist_ok=True)
        (root / dataset / "latest.json").write_bytes(canonical_json(
            {"trade_date": date, "version": version, "manifest": str(run / "manifest.json")}) + b"\n")
    cat_path.write_bytes(canonical_json(cat) + b"\n")
    return run


def kline_row(sym, **kw):
    row = {"symbol": sym, "code": f"sz.{sym}", "date": DATE, "open": 10.0, "high": 10.5, "low": 9.8, "close": 10.2,
           "pre_close": 10.0, "pct_chg": 2.0, "volume": 1000000.0, "amount_yuan": 10200000.0,
           "turnover": 1.5, "turnover_pct": 1.5, "suspended": False, "mismatch": False,
           "universe_version": "sha256-x"}
    row.update(kw)
    return row


def synthetic_rows():
    rows = [kline_row(f"{i:06d}") for i in range(1, 51)]
    rows.append(kline_row("000900", suspended=True, volume=None, amount_yuan=None, pct_chg=None, turnover=None,
                          turnover_pct=None, close=10.0, open=10.0, high=10.0, low=10.0))
    return sorted(rows, key=lambda r: r["symbol"])


def universe_rows_for(rows):
    return [{"symbol": r["symbol"], "trade_status": "0" if r.get("suspended") else "1"} for r in rows]


def build(root: Path, rows, universe_rows=None, **kw):
    manifest_extra = kw.pop("manifest_extra", {})
    uni = universe_rows if universe_rows is not None else universe_rows_for(rows)
    write_partition(root, "symbol_universe", uni, {"quality": {}, "field_units": {}}, source="baostock")
    active = sum(1 for u in uni if u["trade_status"] != "0")
    present = {r["symbol"] for r in rows}
    cover = len({u["symbol"] for u in uni if u["trade_status"] != "0"} & present) / active
    manifest_extra.setdefault("quality", {
        "coverage": cover, "expected_basis": "universe_active", "expected_count": active,
        "missing": active - len({u["symbol"] for u in uni if u["trade_status"] != "0"} & present),
        "field_completeness": {}, "partial": cover < 1, "dropped_outside_universe": 0, "dropped_duplicates": 0,
        "suspended_in_universe": sum(1 for u in uni if u["trade_status"] == "0")})
    return write_partition(root, "daily_kline", rows, manifest_extra, **kw)


LEGACY_SPEC_PATH = (REPO / "src" / "engine_core" / "data_pipeline" / "dataset_specs" / "daily_kline_legacy.json")


def run_check(root, **kw):
    # These synthetic/real-copy partitions use the raw percent layout (pre-canonical schema, C540):
    # they are checked against the legacy spec. The canonical layout is covered in
    # test_daily_kline_manifest_contract.py (C576).
    kw.setdefault("spec", load_spec("daily_kline", LEGACY_SPEC_PATH))
    return check_partition(root, "daily_kline", DATE, **kw)


def failed_ids(report):
    return {c["id"] for c in report.checks if c["status"] == "FAIL"}


def rewrite_manifest(run: Path, fn):
    m = json.loads((run / "manifest.json").read_text())
    fn(m)
    (run / "manifest.json").write_bytes(canonical_json(m) + b"\n")


def rewrite_rows(run: Path, rows, raw=None):
    (run / "rows.jsonl.gz").write_bytes(gzip.compress(raw if raw is not None else _payload(rows), mtime=0))


@pytest.fixture
def clean(tmp_path):
    rows = synthetic_rows()
    run = build(tmp_path, rows)
    return tmp_path, run, rows


# ------------------------------------------------------------------ positive
def test_conformant_partition_passes(clean):
    root, _, _ = clean
    report = run_check(root)
    assert report.passed, report.summary()
    assert report.to_dict()["counts"]["FAIL"] == 0
    assert report.stats["recomputed"]["coverage"] == 1.0


def test_cli_exit_codes_and_json(clean, tmp_path):
    root, run, _ = clean
    cmd = [sys.executable, str(REPO / "examples" / "check_dataset_contract.py"), "--dataset", "daily_kline",
           "--date", DATE, "--root", str(root), "--spec", str(LEGACY_SPEC_PATH)]
    ok = subprocess.run(cmd, capture_output=True, text=True)
    assert ok.returncode == 0, ok.stderr
    assert json.loads(ok.stdout)["ok"] is True
    rewrite_manifest(run, lambda m: m.update(row_count=1))
    bad = subprocess.run(cmd, capture_output=True, text=True)
    assert bad.returncode == 1
    assert "A6" in {v["id"] for v in json.loads(bad.stdout)["violations"]}
    usage = subprocess.run(cmd[:-2] + ["--dataset", "nope", "--root", str(root)], capture_output=True, text=True)
    assert usage.returncode == 2


def test_missing_partition_fails(tmp_path):
    report = run_check(tmp_path)
    assert not report.passed and failed_ids(report) == {"A0"}


# ------------------------------------------------------------------ A: catalog / manifest (mutations)
def test_unit_swap_fails(clean):
    root, run, _ = clean
    rewrite_manifest(run, lambda m: m["field_units"].update(volume="hands"))
    r = run_check(root)
    assert "A11" in failed_ids(r)
    assert "volume" in next(c for c in r.checks if c["id"] == "A11")["details"]["wrong"]


def test_missing_manifest_field_fails(clean):
    root, run, _ = clean
    rewrite_manifest(run, lambda m: m.pop("provider_chain"))
    r = run_check(root)
    assert "A3" in failed_ids(r)
    assert "provider_chain" in next(c for c in r.checks if c["id"] == "A3")["details"]["missing"]


def test_missing_quality_subfield_and_attempt_shape_fail(clean):
    root, run, _ = clean
    rewrite_manifest(run, lambda m: (m["quality"].pop("expected_basis"), m["provider_attempts"][0].pop("reason")))
    assert {"A3b", "A3c"} <= failed_ids(run_check(root))


def test_chain_label_as_source_fails(tmp_path):
    rows = synthetic_rows()
    build(tmp_path, rows, source="baostock>wencai>q2_final", manifest_extra={"provider": "baostock",
          "source": "baostock>wencai>q2_final", "provider_chain": ["baostock", "wencai", "q2_final"]})
    r = run_check(tmp_path)
    assert "A2" in failed_ids(r)
    assert "chain label" in next(c for c in r.checks if c["id"] == "A2")["message"]


def test_provider_not_in_chain_fails(clean):
    root, run, _ = clean
    rewrite_manifest(run, lambda m: m.update(provider_chain=["wencai"]))
    assert "A3d" in failed_ids(run_check(root))


def test_row_count_mismatch_fails(clean):
    root, run, _ = clean
    rewrite_manifest(run, lambda m: m.update(row_count=999))
    assert "A6" in failed_ids(run_check(root))


def test_status_degraded_inconsistent_fails(clean):
    root, run, _ = clean
    rewrite_manifest(run, lambda m: m.update(degraded=True))
    assert "A5" in failed_ids(run_check(root))
    rewrite_manifest(run, lambda m: m.update(business_status="GREAT", degraded=False))
    assert "A5" in failed_ids(run_check(root))


def test_version_not_content_hash_fails(clean):
    root, run, _ = clean
    rewrite_manifest(run, lambda m: m.update(sha256="0" * 64))
    assert "A7" in failed_ids(run_check(root))


def test_dir_version_mismatch_fails(clean):
    root, run, _ = clean
    rewrite_manifest(run, lambda m: m.update(version="sha256-" + "a" * 16))
    ids = failed_ids(run_check(root))
    assert "A2b" in ids and "A7" in ids


def test_identity_fields_fail(clean):
    root, run, _ = clean
    rewrite_manifest(run, lambda m: m.update(trade_date="2026-10-26"))
    assert "A4" in failed_ids(run_check(root))


def test_no_look_ahead_fails(clean):
    root, run, _ = clean
    rewrite_manifest(run, lambda m: m.update(available_at=f"{DATE}T14:00:00+08:00",
                                             fetched_at=f"{DATE}T13:00:00+08:00"))
    assert "A10" in failed_ids(run_check(root))


def test_available_before_as_of_or_fetch_fails(clean):
    root, run, _ = clean
    rewrite_manifest(run, lambda m: m.update(available_at="2026-10-26T20:00:00+08:00"))
    assert {"A9", "A9b"} <= failed_ids(run_check(root))


def test_naive_time_fails(clean):
    root, run, _ = clean
    rewrite_manifest(run, lambda m: m.update(fetched_at="2026-10-27T18:10:00"))
    assert "A8" in failed_ids(run_check(root))


def test_provider_not_allowed_fails(tmp_path):
    build(tmp_path, synthetic_rows(), source="mystery", manifest_extra={"provider_chain": ["mystery"]})
    assert "A2c" in failed_ids(run_check(tmp_path))


# ------------------------------------------------------------------ B: rows
def _mutate_rows(clean, fn):
    root, run, rows = clean
    rows = copy.deepcopy(rows)
    fn(rows)
    raw = _payload(rows)
    rewrite_rows(run, rows, raw)
    return root, run, rows


def test_symbol_format_fails(clean):
    def fn(rows):
        rows[0]["symbol"] = "sz.000001"
    root, *_ = _mutate_rows(clean, fn)
    assert "B1" in failed_ids(run_check(root))


def test_symbol_int_fails(clean):
    def fn(rows):
        rows[1]["symbol"] = 2
    root, *_ = _mutate_rows(clean, fn)
    assert "B1" in failed_ids(run_check(root))


def test_date_not_trade_date_fails(clean):
    def fn(rows):
        rows[3]["date"] = "2026-10-28"
    root, *_ = _mutate_rows(clean, fn)
    assert "B2" in failed_ids(run_check(root))


def test_duplicate_primary_key_fails(clean):
    def fn(rows):
        rows.insert(1, dict(rows[0]))
    root, *_ = _mutate_rows(clean, fn)
    assert "B3" in failed_ids(run_check(root))


def test_unsorted_rows_fail(clean):
    def fn(rows):
        rows.reverse()
    root, *_ = _mutate_rows(clean, fn)
    ids = failed_ids(run_check(root))
    assert "B3b" in ids


def test_zero_impersonating_null_fails(clean):
    def fn(rows):
        next(r for r in rows if r["suspended"])["volume"] = 0.0
        rows[0]["close"] = 0.0
    root, *_ = _mutate_rows(clean, fn)
    r = run_check(root)
    ids = {c["name"] for c in r.checks if c["status"] == "FAIL"}
    assert {"zero_not_null_volume", "zero_not_null_close"} <= ids


def test_null_in_non_nullable_and_all_null_fail(clean):
    def fn(rows):
        rows[0]["close"] = None
        for r in rows:
            r["turnover"] = None
    root, *_ = _mutate_rows(clean, fn)
    ids = failed_ids(run_check(root))
    assert {"B6", "B6b"} <= ids


def test_type_range_enum_fail(clean):
    def fn(rows):
        rows[0]["open"] = "10.0"
        rows[1]["pct_chg"] = 5000.0
        rows[2]["suspended"] = 1
    root, *_ = _mutate_rows(clean, fn)
    ids = failed_ids(run_check(root))
    assert {"B5", "B7"} <= ids


def test_unspecified_and_missing_columns_fail(clean):
    def fn(rows):
        rows[0]["surprise"] = 1
        for r in rows:
            r.pop("mismatch")
    root, *_ = _mutate_rows(clean, fn)
    assert {"B4", "B4b"} <= failed_ids(run_check(root))


def test_enum_check_on_draft_spec(tmp_path):
    spec = {"dataset": "toy", "status": "t", "primary_key": ["symbol"], "symbol_column": "symbol",
            "coverage": {"basis": "none"},
            "columns": {"symbol": {"type": "str"}, "date": {"type": "date"},
                        "w": {"type": "int", "enum": [5, 10]}}}
    rows = [{"symbol": "000001", "date": DATE, "w": 10}, {"symbol": "000002", "date": DATE, "w": 7}]
    write_partition(tmp_path, "toy", rows, {"quality": {"expected_basis": "none"}})
    r = check_partition(tmp_path, "toy", DATE, spec=spec)
    assert "B7b" in failed_ids(r)


# ------------------------------------------------------------------ C: coverage
def test_declared_coverage_lie_fails(clean):
    root, run, _ = clean
    rewrite_manifest(run, lambda m: m["quality"].update(coverage=0.5, missing=7))
    r = run_check(root)
    assert "C2" in failed_ids(r)


def test_missing_symbols_recomputed(tmp_path):
    rows = synthetic_rows()
    uni = universe_rows_for(rows) + [{"symbol": "000777", "trade_status": "1"}]
    run = build(tmp_path, rows, universe_rows=uni)
    # builder computed the honest value (50/51): passes
    r = run_check(tmp_path)
    assert r.passed, r.summary()
    assert r.stats["recomputed"]["missing"] == 1
    rewrite_manifest(run, lambda m: m["quality"].update(coverage=1.0, missing=0))
    assert "C2" in failed_ids(run_check(tmp_path))


def test_row_outside_universe_fails(tmp_path):
    rows = synthetic_rows() + [kline_row("999999")]
    build(tmp_path, rows, universe_rows=universe_rows_for(synthetic_rows()))
    assert "C3" in failed_ids(run_check(tmp_path))


def test_wrong_expected_basis_and_missing_drop_counts_fail(clean):
    root, run, _ = clean
    rewrite_manifest(run, lambda m: (m["quality"].update(expected_basis="all_rows"),
                                     m["quality"].pop("dropped_duplicates")))
    assert {"C1", "C4"} <= failed_ids(run_check(root))


def test_coverage_unverifiable_without_universe_is_warning(tmp_path):
    rows = synthetic_rows()
    write_partition(tmp_path, "daily_kline", rows)
    r = run_check(tmp_path)
    assert any(c["id"] == "C2" and c["status"] == "WARN" for c in r.checks)


# ------------------------------------------------------------------ D: idempotency
def test_non_canonical_serialization_fails(clean):
    root, run, rows = clean
    raw = b"\n".join(json.dumps(r, ensure_ascii=False).encode() for r in rows) + b"\n"
    rewrite_rows(run, rows, raw)
    ids = failed_ids(run_check(root))
    assert "D1" in ids and "A7" in ids


# ------------------------------------------------------------------ E: versions / latest
def test_formal_over_lower_quality_selected_and_locate_flagged(tmp_path):
    rows = synthetic_rows()
    build(tmp_path, rows)
    # a later-appended PROVISIONAL version of the same day (different content)
    low = [dict(r) for r in rows]
    low[0]["close"] = 11.0
    write_partition(tmp_path, "daily_kline", low, {"provider_chain": ["q2_final"]}, source="q2_final",
                    status="PROVISIONAL", make_latest=False)
    r = run_check(tmp_path)
    assert r.meta["source"] == "baostock"          # FORMAL chosen over the later PROVISIONAL
    assert "E1" not in failed_ids(r)
    assert "E2" in failed_ids(r)                    # but ArtifactCatalog.locate returns the last appended
    assert r.stats["multi_version_key_overlap"]["best_keys"] == len(rows)
    low_check = run_check(tmp_path, version=next(e["version"] for e in json.loads(
        (tmp_path / "catalog.json").read_text())["datasets"]["daily_kline"]["dates"][DATE]
        if e["source"] == "q2_final"))
    assert "E1" in failed_ids(low_check)


def _point_latest(root, version, date=DATE):
    cat = json.loads((root / "catalog.json").read_text())
    cat["datasets"]["daily_kline"]["latest"] = {"trade_date": date, "version": version}
    (root / "catalog.json").write_text(json.dumps(cat))


def test_latest_advanced_by_provisional_ok_when_nothing_better_exists(tmp_path):
    # design/24 (2026-10-28): the 15:05 close data may hold latest until the formal version arrives
    build(tmp_path, synthetic_rows(), status="PROVISIONAL", make_latest=False)
    cat = json.loads((tmp_path / "catalog.json").read_text())
    _point_latest(tmp_path, cat["datasets"]["daily_kline"]["dates"][DATE][0]["version"])
    assert "E3" not in failed_ids(run_check(tmp_path))


def test_latest_on_provisional_while_formal_exists_same_day_fails(tmp_path):
    rows = synthetic_rows()
    build(tmp_path, rows)  # FORMAL, latest
    low = [dict(r) for r in rows]
    low[0]["close"] = 11.0
    run = write_partition(tmp_path, "daily_kline", low, {"provider_chain": ["q2_final"]}, source="q2_final",
                          status="PROVISIONAL", make_latest=False)
    _point_latest(tmp_path, run.name)
    assert "E3" in failed_ids(run_check(tmp_path))


def test_latest_on_provisional_while_formal_exists_on_newer_day_fails(tmp_path):
    rows = synthetic_rows()
    build(tmp_path, rows, status="PROVISIONAL", make_latest=False)
    cat = json.loads((tmp_path / "catalog.json").read_text())
    v = cat["datasets"]["daily_kline"]["dates"][DATE][0]["version"]
    cat["datasets"]["daily_kline"]["dates"]["2026-10-28"] = [
        {"version": "sha256-n", "manifest": "x", "business_status": "FORMAL", "source": "baostock"}]
    (tmp_path / "catalog.json").write_text(json.dumps(cat))
    _point_latest(tmp_path, v)
    assert "E3" in failed_ids(run_check(tmp_path))


def test_partial_status_requires_degraded_and_quality_partial(tmp_path):
    rows = synthetic_rows()
    ok = build(tmp_path, rows, status="PROVISIONAL", make_latest=False,
               manifest_extra={"business_status": "PARTIAL", "degraded": True})
    rewrite_manifest(ok, lambda m: m["quality"].update(partial=True))
    assert "A5" not in failed_ids(run_check(tmp_path))
    rewrite_manifest(ok, lambda m: m.update(degraded=False))
    assert "A5" in failed_ids(run_check(tmp_path))
    rewrite_manifest(ok, lambda m: m.update(degraded=True) or m["quality"].update(partial=False))
    assert "A5" in failed_ids(run_check(tmp_path))


def test_latest_sample_authority_fails(clean):
    root, run, _ = clean
    rewrite_manifest(run, lambda m: m.update(authority="SAMPLE"))
    assert "E3" in failed_ids(run_check(root))


def test_latest_json_pointer_mismatch_fails(clean):
    root, run, _ = clean
    (root / "daily_kline" / "latest.json").write_text(json.dumps({"trade_date": "2026-10-20", "version": "sha256-x"}))
    assert "E5" in failed_ids(run_check(root))


# ------------------------------------------------------------------ specs
def test_all_bundled_specs_load_and_are_wellformed():
    for name in ("daily_kline", "daily_indicators", "daily_report_features", "limit_up_pool", "limit_reasons",
                 "plate_members", "plate_registry", "stock_static"):
        spec = load_spec(name)
        assert spec["dataset"] == name and spec["columns"] and "coverage" in spec
        for col, d in spec["columns"].items():
            assert d["type"] in ("str", "float", "int", "bool", "list", "dict", "date"), (name, col)
        if name not in ("daily_kline", "daily_indicators", "daily_report_features"):
            assert spec["status"].startswith("DRAFT")


# ------------------------------------------------------------------ real-partition copy mutations
def _real_copy(tmp_path):
    cat = json.loads((REAL_ROOT / "catalog.json").read_text())["datasets"]
    try:
        ent = cat["daily_kline"]["dates"][DATE][-1]
        uni = cat["symbol_universe"]["dates"][DATE][-1]
    except KeyError:
        pytest.skip("real 2026-10-27 partitions absent")
    from engine_core.data_pipeline.storage import ArtifactCatalog
    catalog = ArtifactCatalog(REAL_ROOT)
    rows = catalog.read_rows(Path(ent["manifest"]))
    urows = catalog.read_rows(Path(uni["manifest"]))
    # repaired, conformant copy of the real rows in a temp root: the checker's baseline must pass
    run = build(tmp_path, rows, universe_rows=[{"symbol": u["symbol"], "trade_status": u.get("trade_status")}
                                               for u in urows])
    return tmp_path, run, rows


def test_real_partition_copy_baseline_passes_then_mutations_fail(tmp_path):
    root, run, rows = _real_copy(tmp_path)
    base = run_check(root)
    assert base.passed, base.summary()
    pristine_manifest = (run / "manifest.json").read_bytes()
    pristine_rows = (run / "rows.jsonl.gz").read_bytes()

    def reset():
        (run / "manifest.json").write_bytes(pristine_manifest)
        (run / "rows.jsonl.gz").write_bytes(pristine_rows)

    cases = {
        "unit swap": ("A11", lambda: rewrite_manifest(run, lambda m: m["field_units"].update(amount_yuan="milli_yuan"))),
        "delete manifest field": ("A3", lambda: rewrite_manifest(run, lambda m: m.pop("field_units"))),
        "row_count": ("A6", lambda: rewrite_manifest(run, lambda m: m.update(row_count=len(rows) - 1))),
        "source chain label": ("A2", lambda: rewrite_manifest(run, lambda m: m.update(provider="baostock>wencai"))),
    }
    for label, (cid, mutate) in cases.items():
        reset()
        mutate()
        assert cid in failed_ids(run_check(root)), label

    def sym():
        bad = copy.deepcopy(rows)
        bad[0]["symbol"] = bad[0]["code"]
        rewrite_rows(run, bad)

    def zero_for_null():
        bad = copy.deepcopy(rows)
        target = next(r for r in bad if r["volume"] is None)
        target["volume"] = 0.0
        rewrite_rows(run, bad)

    for label, cid, mutate in (("symbol format", "B1", sym), ("0 for null", "B8", zero_for_null)):
        reset()
        mutate()
        assert cid in failed_ids(run_check(root)), label
