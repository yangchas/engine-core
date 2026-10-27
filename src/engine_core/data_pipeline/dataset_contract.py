"""Executable dataset contract (fw/design/24-DATASET-CONTRACT.md).

Dataset-agnostic: all per-dataset knowledge lives in ``dataset_specs/<dataset>.json``.
Read-only: never writes to the root it checks.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import math
import re
from datetime import datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .storage import ArtifactCatalog, canonical_json

SPEC_DIR = Path(__file__).with_name("dataset_specs")
_SHANGHAI = ZoneInfo("Asia/Shanghai")
QUALITY_RANK = {"FORMAL": 3, "PROVISIONAL": 2, "DEGRADED": 1}
STATUSES = ("FORMAL", "PROVISIONAL", "DEGRADED")
MANIFEST_REQUIRED = (
    "schema_version", "dataset", "trade_date", "source", "provider", "provider_chain",
    "provider_attempts", "available_at", "fetched_at", "as_of", "business_status", "degraded",
    "row_count", "version", "quality", "field_units")
QUALITY_REQUIRED = ("coverage", "expected_basis", "expected_count", "missing",
                    "field_completeness", "partial")
ATTEMPT_REQUIRED = ("provider", "result", "elapsed_ms", "reason", "row_count")
_VERSION_RE = re.compile(r"^sha256-[0-9a-f]{16}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SYMBOL_RE = re.compile(r"^\d{6}$")


class Report:
    def __init__(self, dataset: str, trade_date: str, root: Path):
        self.meta: dict[str, Any] = {"dataset": dataset, "trade_date": trade_date, "root": str(root)}
        self.checks: list[dict[str, Any]] = []
        self.stats: dict[str, Any] = {}

    def add(self, check_id: str, name: str, status: str, message: str = "", **details: Any) -> None:
        item = {"id": check_id, "name": name, "status": status, "message": message}
        if details:
            item["details"] = details
        self.checks.append(item)

    def ok(self, cid: str, name: str, msg: str = "", **d: Any) -> None:
        self.add(cid, name, "PASS", msg, **d)

    def fail(self, cid: str, name: str, msg: str, **d: Any) -> None:
        self.add(cid, name, "FAIL", msg, **d)

    def warn(self, cid: str, name: str, msg: str, **d: Any) -> None:
        self.add(cid, name, "WARN", msg, **d)

    def skip(self, cid: str, name: str, msg: str) -> None:
        self.add(cid, name, "SKIP", msg)

    def result(self, check: bool, cid: str, name: str, msg_fail: str, **d: Any) -> bool:
        (self.ok(cid, name, **d) if check else self.fail(cid, name, msg_fail, **d))
        return check

    @property
    def passed(self) -> bool:
        return not any(c["status"] == "FAIL" for c in self.checks)

    def to_dict(self) -> dict[str, Any]:
        counts = {s: sum(1 for c in self.checks if c["status"] == s) for s in ("PASS", "FAIL", "WARN", "SKIP")}
        return {**self.meta, "ok": self.passed, "counts": counts, "stats": self.stats,
                "violations": [c for c in self.checks if c["status"] == "FAIL"],
                "warnings": [c for c in self.checks if c["status"] == "WARN"],
                "checks": self.checks}

    def summary(self) -> str:
        d = self.to_dict()
        m = self.meta
        lines = [f"[{'PASS' if d['ok'] else 'FAIL'}] {m['dataset']} {m['trade_date']} "
                 f"version={m.get('version')} source={m.get('source')} spec={m.get('spec_status')} "
                 f"counts={d['counts']}"]
        for c in self.checks:
            if c["status"] in ("FAIL", "WARN"):
                lines.append(f"  {c['status']} {c['id']} {c['name']}: {c['message']}")
        return "\n".join(lines)


def load_spec(dataset: str, path: str | Path | None = None) -> dict[str, Any]:
    spec_path = Path(path) if path else SPEC_DIR / f"{dataset}.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    if spec.get("dataset") != dataset:
        raise ValueError(f"spec {spec_path} is for {spec.get('dataset')!r}, not {dataset!r}")
    return spec


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _rank(status: Any) -> int:
    return QUALITY_RANK.get(status, 0)


def select_version(entries: list[dict[str, Any]], version: str | None = None) -> dict[str, Any] | None:
    """Highest quality version; ties go to the most recently appended."""
    if version:
        return next((e for e in entries if e.get("version") == version), None)
    best = None
    for e in entries:
        if best is None or _rank(e.get("business_status")) >= _rank(best.get("business_status")):
            best = e
    return best


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


_TYPE_CHECK = {
    "str": lambda v: isinstance(v, str),
    "float": _is_number,
    "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "bool": lambda v: isinstance(v, bool),
    "list": lambda v: isinstance(v, list),
    "dict": lambda v: isinstance(v, dict),
    "date": lambda v: isinstance(v, str) and bool(_DATE_RE.match(v)),
}


def _read_rows_raw(rows_path: Path) -> bytes:
    opener = gzip.open if rows_path.suffix == ".gz" else open
    with opener(rows_path, "rb") as handle:
        return handle.read()


def _examples(items: list[Any], n: int = 5) -> list[Any]:
    return items[:n]


# Checks a pre-C576 daily_kline partition cannot satisfy: its manifest never carried the contract fields.
LEGACY_WAIVABLE = ("A2", "A3", "A3c", "A3d", "A5", "A11", "C4")


def apply_provider_overrides(spec: dict[str, Any], provider: Any) -> dict[str, Any]:
    """Merge ``provider_overrides[provider].columns`` into the column specs (e.g. q2_final has no open)."""
    override = (spec.get("provider_overrides") or {}).get(provider)
    if not override:
        return spec
    merged = dict(spec)
    cols = {name: dict(col) for name, col in spec.get("columns", {}).items()}
    for name, patch in (override.get("columns") or {}).items():
        cols[name] = {**cols.get(name, {}), **patch}
    merged["columns"] = cols
    return merged


def check_partition(root: str | Path, dataset: str, trade_date: str, *, version: str | None = None,
                    spec: dict[str, Any] | None = None, legacy_tolerant: bool = False) -> Report:
    """``legacy_tolerant`` (read-only switch, default off): a partition whose manifest has no
    ``provider_chain`` predates C576; it is checked against ``<dataset>_legacy.json`` (when present and no
    explicit spec was given) and its manifest-structure FAILs (LEGACY_WAIVABLE) are reported as WARN.
    Current partitions are checked exactly as without the switch."""
    root = Path(root)
    explicit_spec = spec is not None
    report = Report(dataset, trade_date, root)
    spec = spec or load_spec(dataset)
    report.meta["spec_status"] = spec.get("status", "UNKNOWN")
    catalog = ArtifactCatalog(root)
    cat = catalog.read()
    entry = cat.get("datasets", {}).get(dataset) or {}
    entries = (entry.get("dates") or {}).get(trade_date) or []
    chosen = select_version(entries, version)
    if chosen is None:
        report.fail("A0", "partition_exists", f"no catalog entry for {dataset} {trade_date}"
                    + (f" version {version}" if version else ""))
        return report
    report.meta["version"] = chosen.get("version")
    report.meta["source"] = chosen.get("source")
    manifest_path = Path(chosen["manifest"])
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        report.fail("A0", "manifest_readable", f"{manifest_path}: {exc}")
        return report
    legacy = bool(legacy_tolerant and "provider_chain" not in manifest)
    if legacy:
        report.meta["legacy_tolerant"] = True
        legacy_path = SPEC_DIR / f"{dataset}_legacy.json"
        if not explicit_spec and legacy_path.is_file():
            spec = load_spec(dataset, legacy_path)
            report.meta["spec_status"] = spec.get("status", "UNKNOWN")
    spec = apply_provider_overrides(spec, manifest.get("provider"))
    rows_path = Path(manifest.get("rows_uri") or manifest_path.with_name("rows.jsonl.gz"))
    try:
        raw = _read_rows_raw(rows_path)
        rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    except (OSError, ValueError) as exc:
        report.fail("A0", "rows_readable", f"{rows_path}: {exc}")
        return report
    report.stats["row_count"] = len(rows)

    _check_catalog_and_path(report, root, dataset, trade_date, chosen, manifest_path, rows_path, manifest, spec)
    _check_manifest(report, dataset, trade_date, chosen, manifest, rows, raw, spec)
    _check_rows(report, trade_date, manifest, rows, spec)
    _check_coverage(report, catalog, trade_date, manifest, rows, spec)
    _check_idempotency(report, manifest, rows, raw, spec)
    _check_versions(report, cat, catalog, dataset, trade_date, entries, chosen, root, spec)
    if legacy:
        waived = []
        for check in report.checks:
            if check["status"] == "FAIL" and check["id"] in LEGACY_WAIVABLE:
                check["status"] = "WARN"
                check["message"] = "LEGACY-WAIVED (pre-C576 partition, never rewritten): " + check["message"]
                waived.append(check["id"])
        report.meta["legacy_waived"] = sorted(set(waived))
    return report


# ---------------------------------------------------------------- A: catalog / path / manifest
def _check_catalog_and_path(rep: Report, root: Path, dataset: str, trade_date: str, chosen: dict[str, Any],
                            manifest_path: Path, rows_path: Path, manifest: dict[str, Any],
                            spec: dict[str, Any]) -> None:
    try:
        rel = manifest_path.relative_to(root)
        parts = rel.parts
    except ValueError:
        parts = ()
    layout_ok = (len(parts) == 5 and parts[0] == dataset and parts[1] == f"trade_date={trade_date}"
                 and parts[2].startswith("source=") and _VERSION_RE.match(parts[3] or "")
                 and parts[4] == "manifest.json")
    rep.result(bool(layout_ok), "A1", "path_layout",
               f"manifest path {manifest_path} is not <root>/<dataset>/trade_date=D/source=X/sha256-<16hex>/manifest.json")
    rep.result(rows_path.is_file() and rows_path.parent == manifest_path.parent, "A1b", "rows_file_beside_manifest",
               f"rows file {rows_path} missing or not beside manifest")
    if layout_ok:
        dir_source = parts[2][len("source="):]
        provider = manifest.get("provider")
        rep.result(dir_source == provider and ">" not in dir_source, "A2", "source_dir_equals_provider",
                   f"directory source={dir_source!r} must equal manifest.provider={provider!r} "
                   "(actual successful provider, never a chain label; chain goes to provider_chain)",
                   dir_source=dir_source, provider=provider)
        rep.result(parts[3] == manifest.get("version"), "A2b", "dir_version_equals_manifest_version",
                   f"directory {parts[3]} != manifest.version {manifest.get('version')}")
    allowed = spec.get("allowed_providers")
    if allowed:
        rep.result(manifest.get("provider") in allowed, "A2c", "provider_allowed",
                   f"provider {manifest.get('provider')!r} not in spec allowed_providers {allowed}")


def _check_manifest(rep: Report, dataset: str, trade_date: str, chosen: dict[str, Any], m: dict[str, Any],
                    rows: list[dict[str, Any]], raw: bytes, spec: dict[str, Any]) -> None:
    missing = [k for k in MANIFEST_REQUIRED if k not in m]
    rep.result(not missing, "A3", "manifest_required_fields",
               f"missing manifest fields: {missing}", missing=missing)
    q = m.get("quality") if isinstance(m.get("quality"), dict) else {}
    qmissing = [k for k in QUALITY_REQUIRED if k not in q]
    rep.result(isinstance(m.get("quality"), dict) and not qmissing, "A3b", "manifest_quality_fields",
               f"manifest.quality missing/invalid: {qmissing or 'not an object'}", missing=qmissing)
    attempts = m.get("provider_attempts")
    bad_attempts = []
    if isinstance(attempts, list):
        for i, a in enumerate(attempts):
            miss = [k for k in ATTEMPT_REQUIRED if not isinstance(a, dict) or k not in a]
            if miss:
                bad_attempts.append({"index": i, "missing": miss})
    rep.result(isinstance(attempts, list) and bool(attempts) and not bad_attempts, "A3c", "provider_attempts_shape",
               "provider_attempts absent/empty or items missing required keys",
               bad=_examples(bad_attempts))
    chain = m.get("provider_chain")
    rep.result(isinstance(chain, list) and bool(chain) and m.get("provider") in chain, "A3d",
               "provider_chain_contains_provider",
               f"provider_chain {chain!r} must be a non-empty list containing provider {m.get('provider')!r}")
    rep.result(m.get("dataset") == dataset and m.get("trade_date") == trade_date, "A4", "identity_fields",
               f"manifest dataset/trade_date = {m.get('dataset')!r}/{m.get('trade_date')!r}, "
               f"expected {dataset!r}/{trade_date!r}")
    status = m.get("business_status")
    deg = m.get("degraded")
    primary = spec.get("primary_provider")
    expected_deg = (status == "DEGRADED") or (bool(primary) and m.get("provider") != primary)
    consistent = status in STATUSES and isinstance(deg, bool) and deg == expected_deg
    rule = ("true iff business_status == DEGRADED" if not primary else
            f"true iff business_status == DEGRADED or provider != primary provider {primary!r}")
    rep.result(consistent, "A5", "business_status_degraded_consistent",
               f"business_status={status!r} degraded={deg!r}: status must be one of {STATUSES}, "
               f"degraded bool and {rule}",
               business_status=status, degraded=deg)
    rep.result(m.get("row_count") == len(rows) == chosen.get("row_count"), "A6", "row_count_matches",
               f"manifest.row_count={m.get('row_count')} actual rows={len(rows)} catalog={chosen.get('row_count')}")
    payload_sha = hashlib.sha256(raw).hexdigest()
    version_digest = payload_sha
    inputs = m.get("inputs")
    if isinstance(inputs, dict) and inputs:
        identity: dict[str, Any] = {"payload_sha256": payload_sha, "inputs": dict(inputs)}
        if "node_fingerprint" in m:
            identity["node_fingerprint"] = m["node_fingerprint"]
        version_digest = hashlib.sha256(canonical_json(identity)).hexdigest()
    rep.result(m.get("sha256") == payload_sha and m.get("version") == f"sha256-{version_digest[:16]}"
               and chosen.get("sha256") == payload_sha, "A7", "version_is_content_sha256",
               f"manifest.sha256={m.get('sha256')!r} version={m.get('version')!r}; content sha256={payload_sha}")
    _check_times(rep, trade_date, m, spec)
    _check_units(rep, m, spec)


def _check_times(rep: Report, trade_date: str, m: dict[str, Any], spec: dict[str, Any]) -> None:
    parsed = {k: _parse_time(m.get(k)) for k in ("available_at", "fetched_at", "as_of")}
    bad = [k for k, v in parsed.items() if v is None]
    rep.result(not bad, "A8", "times_parseable_with_tz",
               f"unparseable or timezone-less: {bad}", values={k: m.get(k) for k in bad})
    av, fe, ao = parsed["available_at"], parsed["fetched_at"], parsed["as_of"]
    if av is not None and ao is not None:
        rep.result(av >= ao, "A9", "available_at_not_before_as_of",
                   f"available_at {m.get('available_at')} earlier than as_of {m.get('as_of')}")
    if av is not None and fe is not None:
        rep.result(av >= fe, "A9b", "available_at_not_before_fetched_at",
                   f"available_at {m.get('available_at')} earlier than fetched_at {m.get('fetched_at')}")
    earliest = spec.get("earliest_available_local")
    if earliest and av is not None:
        hh, mm = (int(x) for x in earliest.split(":"))
        floor = datetime.combine(datetime.fromisoformat(trade_date).date(), time(hh, mm), tzinfo=_SHANGHAI)
        rep.result(av >= floor, "A10", "no_look_ahead",
                   f"available_at {m.get('available_at')} earlier than {earliest} Asia/Shanghai on {trade_date}: "
                   "end-of-day data cannot be available (or FORMAL) before that",
                   floor=floor.isoformat())
    else:
        rep.skip("A10", "no_look_ahead", "spec declares no earliest_available_local")


def _check_units(rep: Report, m: dict[str, Any], spec: dict[str, Any]) -> None:
    units = m.get("field_units")
    expected = {c: d["unit"] for c, d in spec.get("columns", {}).items() if d.get("unit")}
    if not isinstance(units, dict):
        rep.fail("A11", "field_units_match_spec", "manifest.field_units absent or not an object",
                 expected=expected)
        return
    wrong = {c: {"manifest": units.get(c), "spec": u} for c, u in expected.items() if units.get(c) != u}
    rep.result(not wrong, "A11", "field_units_match_spec",
               f"manifest.field_units disagree with spec for {sorted(wrong)}", wrong=wrong)


# ---------------------------------------------------------------- B: rows
def _check_rows(rep: Report, trade_date: str, m: dict[str, Any], rows: list[dict[str, Any]],
                spec: dict[str, Any]) -> None:
    cols: dict[str, dict[str, Any]] = spec.get("columns", {})
    sym_col = spec.get("symbol_column", "symbol")
    if sym_col:
        bad = [r.get(sym_col) for r in rows if not (isinstance(r.get(sym_col), str)
                                                    and _SYMBOL_RE.match(r[sym_col]))]
        rep.result(not bad, "B1", "symbol_six_digit_string",
                   f"{len(bad)} rows with {sym_col} not a 6-digit string", count=len(bad),
                   examples=_examples(bad))
    else:
        rep.skip("B1", "symbol_six_digit_string", "spec symbol_column is null")
    # `date` column is mandated by contract §3 for every dataset
    bad_dates = [r.get("date") for r in rows if r.get("date") != trade_date]
    rep.result(not bad_dates, "B2", "date_equals_trade_date",
               f"{len(bad_dates)} rows whose `date` != {trade_date} (contract requires a `date` column "
               "equal to the partition trade_date)", count=len(bad_dates), examples=_examples(bad_dates))
    for dc in spec.get("date_columns", []):
        bad2 = [r.get(dc) for r in rows if r.get(dc) != trade_date]
        rep.result(not bad2, "B2b", f"date_column_{dc}_equals_trade_date",
                   f"{len(bad2)} rows whose {dc} != {trade_date}", count=len(bad2), examples=_examples(bad2))
    pk = spec.get("primary_key") or []
    if pk:
        keys = [tuple(json.dumps(r.get(k), sort_keys=True) for k in pk) for r in rows]
        dups = len(keys) - len(set(keys))
        rep.stats["duplicate_primary_keys"] = dups
        rep.result(dups == 0, "B3", "primary_key_unique", f"{dups} duplicate primary keys {pk}", count=dups)
        if spec.get("row_order", "primary_key") == "primary_key":
            ordered = keys == sorted(keys)
            rep.result(ordered, "B3b", "rows_sorted_by_primary_key",
                       "rows are not sorted by primary key (fixed ordering is required for idempotent sha256)")
    else:
        rep.fail("B3", "primary_key_unique", "spec declares no primary_key")
    _check_columns(rep, rows, cols, spec)


def _check_columns(rep: Report, rows: list[dict[str, Any]], cols: dict[str, dict[str, Any]],
                   spec: dict[str, Any]) -> None:
    seen: set[str] = set()
    for r in rows:
        seen.update(r)
    extra = sorted(seen - set(cols))
    rep.result(not extra, "B4", "no_unspecified_columns",
               f"columns present in rows but not in spec: {extra}", extra=extra)
    absent = sorted(c for c, d in cols.items() if c not in seen and not d.get("optional"))
    rep.result(not absent, "B4b", "no_missing_columns",
               f"spec columns absent from every row: {absent}", missing=absent)
    for name, d in cols.items():
        if name not in seen:
            continue
        values = [r.get(name) for r in rows]
        _check_one_column(rep, name, d, rows, values)


def _check_one_column(rep: Report, name: str, d: dict[str, Any], rows: list[dict[str, Any]],
                      values: list[Any]) -> None:
    tcheck = _TYPE_CHECK[d.get("type", "str")]
    nulls = [i for i, v in enumerate(values) if v is None]
    absent_rows = sum(1 for r in rows if name not in r)
    bad_type = [v for v in values if v is not None and not tcheck(v)]
    nonfinite = [v for v in values if isinstance(v, float) and not math.isfinite(v)]
    rep.result(not bad_type and not nonfinite, "B5", f"type_{name}",
               f"{len(bad_type)} values not {d.get('type')} (+{len(nonfinite)} non-finite) in column {name}",
               count=len(bad_type) + len(nonfinite), examples=_examples(bad_type + nonfinite))
    if not d.get("nullable", False):
        n = len(nulls) + absent_rows
        rep.result(n == 0, "B6", f"not_null_{name}", f"{n} null/absent values in non-nullable column {name}",
                   count=n)
    elif nulls and len(nulls) == len(values) and not d.get("all_null_ok", False):
        rep.fail("B6b", f"all_null_{name}", f"column {name} is null in every row (declared nullable but "
                 "carries no data)", count=len(nulls))
    nums = [v for v in values if _is_number(v) and math.isfinite(v)]
    lo, hi = d.get("min"), d.get("max")
    if nums and (lo is not None or hi is not None):
        out = [v for v in nums if (lo is not None and v < lo) or (hi is not None and v > hi)]
        rep.result(not out, "B7", f"range_{name}", f"{len(out)} values of {name} outside [{lo}, {hi}]",
                   count=len(out), examples=_examples(out))
    enum = d.get("enum")
    if enum is not None:
        out = [v for v in values if v is not None and v not in enum]
        rep.result(not out, "B7b", f"enum_{name}", f"{len(out)} values of {name} outside {enum}",
                   count=len(out), examples=_examples(out))
    if d.get("zero_suspicious"):
        excuse = d.get("zero_ok_when") or {}
        zero = [i for i, r in enumerate(rows) if _is_number(r.get(name)) and r[name] == 0
                and not any(r.get(k) == v for k, v in excuse.items())]
        rep.result(not zero, "B8", f"zero_not_null_{name}",
                   f"{len(zero)} rows where {name} == 0 (0 is suspicious: null must not be impersonated by 0)",
                   count=len(zero), row_indexes=_examples(zero))


# ---------------------------------------------------------------- C: coverage
def _check_coverage(rep: Report, catalog: ArtifactCatalog, trade_date: str, m: dict[str, Any],
                    rows: list[dict[str, Any]], spec: dict[str, Any]) -> None:
    cov = spec.get("coverage") or {"basis": "none"}
    basis = cov.get("basis", "none")
    q = m.get("quality") if isinstance(m.get("quality"), dict) else {}
    if basis == "none":
        rep.result(q.get("expected_basis") in (None, "none", "not_applicable"), "C1", "expected_basis_not_applicable",
                   f"spec says no universe coverage but manifest declares expected_basis={q.get('expected_basis')!r}")
        return
    rep.result(q.get("expected_basis") == basis, "C1", "expected_basis_matches_spec",
               f"manifest.quality.expected_basis={q.get('expected_basis')!r}, spec expects {basis!r}")
    udata = cov.get("universe_dataset", "symbol_universe")
    upath = catalog.locate(udata, trade_date)
    if upath is None:
        rep.warn("C2", "coverage_recomputed", f"no {udata} partition for {trade_date}: coverage unverifiable")
        return
    urows = catalog.read_rows(upath)
    sym = spec.get("symbol_column", "symbol")
    by_symbol = {str(r.get("symbol", "")).strip(): r for r in urows}
    by_symbol.pop("", None)
    suspended = {s for s, r in by_symbol.items() if str(r.get("trade_status", "")).strip() == "0"}
    active = set(by_symbol) - suspended
    row_syms = [r.get(sym) for r in rows]
    present = set(row_syms)
    inside = len(active & present)
    recomputed = {
        "expected_count": len(active), "missing": len(active - present),
        "coverage": (inside / len(active)) if active else None,
        "suspended_in_universe": len(suspended),
    }
    outside = len([s for s in row_syms if s not in by_symbol])
    rep.stats.update({"recomputed": recomputed, "rows_outside_universe": outside,
                      "universe_partition": str(upath)})
    diffs = {}
    for key, val in recomputed.items():
        if key not in q:
            continue
        declared = q[key]
        same = (declared == val if not isinstance(val, float) or declared is None
                else _is_number(declared) and abs(declared - val) < 1e-6)
        if not same:
            diffs[key] = {"declared": declared, "recomputed": val}
    rep.result(not diffs and "coverage" in q, "C2", "coverage_recomputed_equals_declared",
               f"declared quality disagrees with recomputation from {udata}: {diffs or 'coverage not declared'}",
               recomputed=recomputed, diffs=diffs)
    rep.result(outside == 0, "C3", "no_rows_outside_universe",
               f"{outside} rows have symbols outside the {trade_date} universe (must be dropped and counted)",
               count=outside)
    declared_counts = [k for k in ("dropped_outside_universe", "dropped_duplicates") if k in q]
    rep.result(len(declared_counts) == 2, "C4", "drop_counts_declared",
               "manifest.quality must carry dropped_outside_universe and dropped_duplicates counts "
               f"(contract §3); present: {declared_counts}")


# ---------------------------------------------------------------- D: idempotency
def _check_idempotency(rep: Report, m: dict[str, Any], rows: list[dict[str, Any]], raw: bytes,
                       spec: dict[str, Any]) -> None:
    payload = b"\n".join(canonical_json(dict(r)) for r in rows) + (b"\n" if rows else b"")
    again = hashlib.sha256(payload).hexdigest()
    rep.result(payload == raw and again == m.get("sha256"), "D1", "reserialize_same_sha256",
               f"re-serialising the parsed rows gives sha256 {again}, manifest.sha256 {m.get('sha256')}",
               reserialized=again)


# ---------------------------------------------------------------- E: multi-version / latest
def _check_versions(rep: Report, cat: dict[str, Any], catalog: ArtifactCatalog, dataset: str, trade_date: str,
                    entries: list[dict[str, Any]], chosen: dict[str, Any], root: Path, spec: dict[str, Any]) -> None:
    rep.stats["versions_for_day"] = [{"version": e.get("version"), "source": e.get("source"),
                                      "business_status": e.get("business_status")} for e in entries]
    best_rank = max(_rank(e.get("business_status")) for e in entries)
    rep.result(_rank(chosen.get("business_status")) == best_rank, "E1", "checked_version_is_best_quality",
               "checked version is not the highest-quality version of the day")
    located = catalog.locate(dataset, trade_date)
    if located is not None and len(entries) > 1:
        rep.result(str(located) == chosen.get("manifest"), "E2", "locate_returns_best_quality",
                   f"ArtifactCatalog.locate returns {located} (last appended) but the best-quality version is "
                   f"{chosen.get('manifest')}: readers using locate see the lower quality version")
    elif len(entries) > 1:
        rep.fail("E2", "locate_returns_best_quality", "locate returned nothing")
    else:
        rep.skip("E2", "locate_returns_best_quality", "single version for the day")
    if len(entries) > 1:
        keyed = _key_overlap(entries, spec, catalog)
        rep.stats["multi_version_key_overlap"] = keyed
    latest = (cat.get("datasets", {}).get(dataset) or {}).get("latest")
    if not latest:
        rep.warn("E3", "latest_not_advanced_by_low_quality", "catalog has no latest pointer for dataset")
        return
    lv = next((e for e in ((cat["datasets"][dataset].get("dates") or {}).get(latest.get("trade_date"), []))
               if e.get("version") == latest.get("version")), None)
    lstatus = (lv or {}).get("business_status")
    lm = {}
    if lv:
        try:
            lm = json.loads(Path(lv["manifest"]).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    sample = lm.get("authority") in ("SAMPLE", "TEST") or lm.get("sample") is True or lm.get("is_sample") is True
    rep.result(lv is not None and lstatus not in ("PROVISIONAL", "DEGRADED") and not sample, "E3",
               "latest_not_advanced_by_low_quality_or_sample",
               f"latest -> {latest} points at business_status={lstatus!r}, sample={sample}: "
               "PROVISIONAL/DEGRADED/sample versions must not advance latest" if lv else
               f"latest -> {latest} not found among catalog versions", latest=latest)
    if latest.get("trade_date") == trade_date and best_rank == QUALITY_RANK["FORMAL"]:
        rep.result(_rank(lstatus) == 3, "E4", "latest_is_formal_when_formal_exists",
                   f"a FORMAL version exists for {trade_date} but latest points at {lstatus!r}")
    pointer = root / dataset / "latest.json"
    if pointer.is_file():
        try:
            p = json.loads(pointer.read_text(encoding="utf-8"))
            rep.result(p.get("trade_date") == latest.get("trade_date") and p.get("version") == latest.get("version"),
                       "E5", "latest_json_matches_catalog",
                       f"{pointer} says {p.get('trade_date')}/{p.get('version')}, catalog.json says "
                       f"{latest.get('trade_date')}/{latest.get('version')}")
        except (OSError, ValueError):
            rep.fail("E5", "latest_json_matches_catalog", f"{pointer} unreadable")


def _key_overlap(entries: list[dict[str, Any]], spec: dict[str, Any],
                 catalog: ArtifactCatalog) -> dict[str, Any]:
    pk = spec.get("primary_key") or []
    if not pk:
        return {}
    per: dict[str, set[Any]] = {}
    for e in entries:
        try:
            rows = catalog.read_rows(Path(e["manifest"]))
        except (OSError, ValueError, KeyError):
            continue
        per[str(e.get("version"))] = {tuple(json.dumps(r.get(k)) for k in pk) for r in rows}
    best = select_version(entries)
    best_keys = per.get(str((best or {}).get("version")), set())
    return {"keys_only_in_lower_quality": {v: len(k - best_keys) for v, k in per.items()
                                           if v != str((best or {}).get("version"))},
            "best_version": (best or {}).get("version"), "best_keys": len(best_keys)}


def latest_trade_date(root: str | Path, dataset: str) -> str | None:
    dates = ((ArtifactCatalog(root).read().get("datasets", {}).get(dataset) or {}).get("dates") or {})
    return max(dates) if dates else None
