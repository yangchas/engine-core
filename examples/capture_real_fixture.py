#!/usr/bin/env python3
"""Capture a RAW upstream response as a real test fixture, with provenance.

Fixtures under tests/fixtures/real/ must come from here (or from copying a committed Core artifact via
--from-catalog); hand-written rows are not accepted by tests/test_real_fixtures.py.

  capture_real_fixture.py ths_hot_rank
  capture_real_fixture.py kaipan_hot_plates [--date YYYY-MM-DD]
  capture_real_fixture.py kaipan_pool [--date YYYY-MM-DD] [--level 1]
  capture_real_fixture.py wencai_daily_kline --date YYYY-MM-DD
  capture_real_fixture.py wencai_limit_up_pool_today --date YYYY-MM-DD
  capture_real_fixture.py q2_limit_up_pool_today --date YYYY-MM-DD
  capture_real_fixture.py baostock_query_all_stock --date YYYY-MM-DD
  capture_real_fixture.py kaipan_plate_members --date YYYY-MM-DD --plate-code 801001
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import shutil
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
REAL = ROOT / "tests" / "fixtures" / "real"
PROVENANCE = REAL / "PROVENANCE.json"
TZ = ZoneInfo("Asia/Shanghai")


def _ths_raw() -> tuple[object, dict]:
    from engine_core.data_pipeline.providers import ths_hot_rank as ths
    params = {"stock_type": "a", "type": "day", "list_type": "tech"}
    url = ths.URL + "?" + urllib.parse.urlencode(params)
    last = None
    for _ in range(4):  # the endpoint is flaky (SSL handshake timeouts observed 2026-10-23)
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=ths.HEADERS), timeout=30) as response:
                return json.loads(response.read().decode("utf-8")), {"endpoint": ths.URL, "params": params}
        except (urllib.error.URLError, TimeoutError) as exc:
            last = exc
            time.sleep(5)
    raise SystemExit(f"capture failed: {last!r}")


def _kaipan(call: str, **kwargs) -> object:
    return getattr(importlib.import_module("pykaipan.pykaipan"), call)(**kwargs)


def _wencai_daily_kline(day: str) -> tuple[object, dict]:
    from engine_core.data_pipeline.providers.wencai_daily_adapter import (
        MAX_PAGES, PAGE_SIZE, build_daily_kline_query, fetch_daily_kline,
    )

    class RateGate:
        def __init__(self):
            self.last_request = 0.0
            self.request_count = 1

        def request(self):
            now = time.monotonic()
            delay = max(0.0, 10.0 - (now - self.last_request)) if self.last_request else 0.0
            if delay:
                time.sleep(delay)
            self.last_request = time.monotonic()
            self.request_count += 1

    gate = RateGate()
    payload = fetch_daily_kline(day, context=gate, page_size=PAGE_SIZE, max_pages=MAX_PAGES)
    params = {"query": build_daily_kline_query(day), "loop": False,
              "page_size": PAGE_SIZE, "max_pages": MAX_PAGES,
              "min_interval_seconds": 10, "max_requests": MAX_PAGES}
    return payload, {"call": "pywencai.wencai.get_robot_data", "params": params,
                     "page_count": payload.get("page_count"),
                     "expected_count": payload.get("expected_count"),
                     "partial": payload.get("partial"),
                     "source_dates": payload.get("source_dates")}


def _wencai_limit_up_pool_today(day: str) -> tuple[object, dict]:
    from engine_core.data_pipeline.providers.limit_up_today_sources import fetch_wencai_today

    payload = fetch_wencai_today(day)
    return payload["records"], {"call": "pywencai.wencai.get_robot_data",
                                "params": {"query": payload["question"], "page": 1,
                                           "perpage": 100, "retry": 0, "max_requests": 1},
                                "request_count": payload["request_count"],
                                "reported_total": payload["reported_total"],
                                "complete": payload["complete"], "truncated": payload["truncated"]}


def _q2_limit_up_pool_today(day: str) -> tuple[object, dict]:
    from engine_core.data_pipeline.providers.limit_up_today_sources import read_q2_snapshot

    payload = read_q2_snapshot(day)
    fields = sorted({field for values in payload["hashes"].values() for field in values})
    return payload, {"call": "Redis SMEMBERS + pipelined HGETALL", "key": f"q2:active:{day.replace('-', '')}",
                     "active_symbol_count": len(payload["active_symbols"]), "field_names": fields,
                     "read_only_commands": ["EXISTS", "SMEMBERS", "HGETALL"]}


def _baostock_query_all_stock(day: str) -> tuple[object, dict]:
    import baostock as bs

    login = bs.login()
    if getattr(login, "error_code", None) != "0":
        raise SystemExit("Baostock login failed")
    try:
        result = bs.query_all_stock(day=day)
        if getattr(result, "error_code", None) != "0":
            raise SystemExit("Baostock query_all_stock failed")
        rows = []
        while result.next():
            rows.append(result.get_row_data())
        fields = getattr(result, "fields", None)
        return {"fields": list(fields) if fields else [], "rows": rows}, {
            "call": "baostock.query_all_stock", "params": {"day": day},
            "request_count": 1, "row_count": len(rows),
        }
    finally:
        bs.logout()


def _baostock_stock_basic() -> tuple[object, dict]:
    """Capture the current Baostock basic-security response (it has no source-date echo)."""
    import baostock as bs

    login = bs.login()
    if getattr(login, "error_code", None) != "0":
        raise SystemExit("Baostock login failed")
    try:
        result = bs.query_stock_basic()
        if getattr(result, "error_code", None) != "0":
            raise SystemExit("Baostock query_stock_basic failed")
        rows = []
        while result.next():
            rows.append(result.get_row_data())
        fields = getattr(result, "fields", None)
        return {"fields": list(fields) if fields else [], "rows": rows}, {
            "call": "baostock.query_stock_basic", "params": {}, "request_count": 1,
            "row_count": len(rows), "source_date_echo": None,
            "snapshot_semantics": "current response; provider supplies no source-date echo",
        }
    finally:
        bs.logout()


def capture(kind: str, day: str, level: int, *, plate_code: str | None = None,
            index: int = 0, page_size: int = 2000) -> tuple[str, object, dict]:
    if kind == "ths_hot_rank":
        raw, meta = _ths_raw()
        return "ths_hot_rank", raw, meta
    if kind == "kaipan_hot_plates":
        params = {"date": day, "index": "0", "size": "50"}
        return "kaipan_hot_plates", _kaipan("getHisPlates", **params), {"call": "getHisPlates", "params": params}
    if kind == "kaipan_pool":
        params = {"date": day, "ban": str(level), "size": "2000"}
        return "kaipan_pool", _kaipan("getHisBans", **params), {"call": "getHisBans", "params": params}
    if kind == "kaipan_plate_members":
        if not plate_code or not plate_code.isdigit():
            raise SystemExit("--plate-code must be supplied for kaipan_plate_members")
        params = {"p": plate_code, "date": day, "index": str(index), "size": str(page_size)}
        raw = _kaipan("getHisPlateIds", **params)
        return "kaipan_plate_members", raw, {
            "call": "getHisPlateIds", "params": params, "request_count": 1,
            "response_count": raw.get("Count") if isinstance(raw, dict) else None,
            "source_date_echo": None,
        }
    if kind == "wencai_daily_kline":
        raw, meta = _wencai_daily_kline(day)
        return "wencai_daily_kline", raw, meta
    if kind == "wencai_limit_up_pool_today":
        raw, meta = _wencai_limit_up_pool_today(day)
        return "wencai_limit_up_pool_today", raw, meta
    if kind == "q2_limit_up_pool_today":
        raw, meta = _q2_limit_up_pool_today(day)
        return "q2_limit_up_pool_today", raw, meta
    if kind == "baostock_query_all_stock":
        raw, meta = _baostock_query_all_stock(day)
        return "baostock_query_all_stock", raw, meta
    if kind == "baostock_stock_basic":
        raw, meta = _baostock_stock_basic()
        return "baostock_stock_basic", raw, meta
    raise SystemExit("unknown kind " + kind)


def copy_catalog_artifact(dataset: str, day: str, version: str, label: str,
                          catalog_root: str | Path) -> list[tuple[str, dict]]:
    """Copy one immutable Core catalog version into the real-fixture index."""
    from engine_core.data_pipeline.storage import ArtifactCatalog

    root = Path(catalog_root).expanduser()
    catalog = ArtifactCatalog(root).read()
    versions = (catalog.get("datasets", {}).get(dataset, {}).get("dates", {}).get(day) or [])
    if isinstance(versions, dict):
        versions = [versions]
    selected = next((entry for entry in versions
                     if isinstance(entry, dict) and entry.get("version") == version), None)
    if selected is None:
        raise SystemExit(f"catalog version not found: {dataset} {day} {version}")
    manifest_source = Path(str(selected.get("manifest") or ""))
    if not manifest_source.is_absolute():
        manifest_source = root / manifest_source
    rows_source = manifest_source.parent / "rows.jsonl.gz"
    if not manifest_source.is_file() or not rows_source.is_file():
        raise SystemExit(f"catalog artifact incomplete: {dataset} {day} {version}")
    manifest = json.loads(manifest_source.read_text(encoding="utf-8"))
    if (manifest.get("dataset") not in (None, dataset)
            or manifest.get("trade_date") not in (None, day)):
        raise SystemExit("catalog manifest identity mismatch")
    if str(manifest.get("version") or selected.get("version")) != version:
        raise SystemExit("catalog manifest version mismatch")
    source_dir = manifest_source.parent.parent
    expected_prefix = root / dataset / f"trade_date={day}"
    selected_source = selected.get("source")
    if (source_dir.parent != expected_prefix
            or not source_dir.name.startswith("source=")
            or (selected_source is not None
                and source_dir.name != f"source={selected_source}")):
        raise SystemExit("catalog artifact path identity mismatch")

    destination_root = REAL / "core_artifacts" / "c594" / dataset / day / label
    destination_root.mkdir(parents=True, exist_ok=True)
    copied_at = datetime.now(TZ).isoformat(timespec="seconds")
    entries = []
    for kind, source, filename in (
        ("core_artifact_manifest", manifest_source, "manifest.json"),
        ("core_artifact_rows", rows_source, "rows.jsonl.gz"),
    ):
        destination = destination_root / filename
        shutil.copyfile(source, destination)
        relative = destination.relative_to(REAL).as_posix()
        command = f"cp {source} tests/fixtures/real/{relative}"
        body = destination.read_bytes()
        entries.append((relative, {
            "bytes": len(body), "command": command, "copied_at": copied_at,
            "dataset": dataset, "kind": kind,
            "sha256": hashlib.sha256(body).hexdigest(),
            "source": str(manifest.get("source") or selected.get("source") or "UNKNOWN"),
            "source_path": str(source), "trade_date": day, "version": version,
        }))
    return entries


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("kind", nargs="?", choices=["ths_hot_rank", "kaipan_hot_plates", "kaipan_pool",
                                                     "kaipan_plate_members",
                                                     "wencai_daily_kline", "wencai_limit_up_pool_today",
                                                     "q2_limit_up_pool_today",
                                                     "baostock_query_all_stock",
                                                     "baostock_stock_basic"])
    parser.add_argument("--date", default=datetime.now(TZ).date().isoformat())
    parser.add_argument("--level", type=int, default=1)
    parser.add_argument("--plate-code")
    parser.add_argument("--index", type=int, default=0)
    parser.add_argument("--page-size", type=int, default=2000)
    parser.add_argument("--from-catalog", action="store_true",
                        help="copy an immutable Core manifest and rows into the fixture tree")
    parser.add_argument("--dataset", choices=["daily_kline", "symbol_universe", "plate_registry",
                                               "plate_members", "stock_static"])
    parser.add_argument("--version", help="exact catalog version to copy")
    parser.add_argument("--label", help="fixture subdirectory label")
    parser.add_argument("--catalog-root", default="~/core-data")
    args = parser.parse_args(argv)
    if args.from_catalog:
        if args.kind or not all((args.dataset, args.version, args.label)):
            parser.error("--from-catalog requires --dataset, --version and --label, and no kind")
        if "/" in args.label or args.label in {".", ".."}:
            parser.error("--label must be one path component")
        entries = copy_catalog_artifact(args.dataset, args.date, args.version, args.label,
                                        args.catalog_root)
        index = json.loads(PROVENANCE.read_text(encoding="utf-8")) if PROVENANCE.exists() else {}
        core_index = index.setdefault("_core_artifacts", {})
        for name, meta in entries:
            core_index[name] = meta
            print(name, meta["bytes"], "bytes")
        PROVENANCE.write_text(json.dumps(index, ensure_ascii=False, indent=1) + "\n",
                              encoding="utf-8")
        return 0
    if not args.kind:
        parser.error("kind is required unless --from-catalog is used")
    fetched_at = datetime.now(TZ).isoformat(timespec="seconds")
    if args.index < 0 or not 1 <= args.page_size <= 2000:
        parser.error("--index must be non-negative and --page-size must be 1..2000")
    source, raw, meta = capture(args.kind, args.date, args.level,
                                plate_code=args.plate_code, index=args.index,
                                page_size=args.page_size)
    body = json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str, indent=1).encode("utf-8") + b"\n"
    level = f"_L{args.level}" if args.kind == "kaipan_pool" else ""
    plate_part = f"_{args.plate_code}_i{args.index}" if args.kind == "kaipan_plate_members" else ""
    name = f"{source}_{args.date}{level}{plate_part}_{fetched_at[11:16].replace(':', '')}.json"
    REAL.mkdir(parents=True, exist_ok=True)
    (REAL / name).write_bytes(body)
    index = json.loads(PROVENANCE.read_text(encoding="utf-8")) if PROVENANCE.exists() else {}
    index[name] = {"source": source, "trade_date": args.date, "fetched_at": fetched_at, "bytes": len(body),
                   "sha256": hashlib.sha256(body).hexdigest(), "kind": "raw_upstream_response",
                   "command": "examples/capture_real_fixture.py " + " ".join(argv or sys.argv[1:]), **meta}
    PROVENANCE.write_text(json.dumps(index, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(name, len(body), "bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
