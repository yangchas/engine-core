#!/usr/bin/env python3
"""Import explicitly selected reference files into immutable Core artifacts."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from engine_core.data_pipeline.storage import ArtifactCatalog


def _json_rows(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return [dict(row) for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        rows = payload.get("rows")
        if isinstance(rows, list):
            return [dict(row) for row in rows if isinstance(row, dict)]
        if isinstance(payload.get("plates"), list):
            return [dict(row) for row in payload["plates"] if isinstance(row, dict)]
        if isinstance(payload.get("response"), dict) and isinstance(payload["response"].get("list"), list):
            return [dict(row, captured_local=payload.get("captured_local")) if isinstance(row, dict)
                    else {"raw": row, "captured_local": payload.get("captured_local")}
                    for row in payload["response"]["list"]]
        return [dict(value, code=key) if isinstance(value, dict) else {"code": key, "value": value}
                for key, value in payload.items() if key not in {"captured_local", "response"}]
    return []


def _rows(path: Path, dataset: str = "") -> list[dict[str, Any]]:
    if path.is_dir():
        if dataset == "plate_registry":
            return _rows(path / "registry.json")
        if dataset == "plate_members":
            return _rows(path / "relation.csv")
        if dataset == "limit_reasons":
            rows = []
            for item in sorted(path.glob("reasons/*.json")):
                payload = json.loads(item.read_text(encoding="utf-8"))
                symbol = str(payload.get("StockID") or item.stem).zfill(6)
                for reason in payload.get("List", []):
                    if isinstance(reason, dict):
                        rows.append({**reason, "symbol": symbol,
                                     "event_date": reason.get("Date") or reason.get("date")})
            return rows
        if dataset == "limit_up_pool":
            rows = []
            for item in sorted(path.glob("pools/*_ban*.json")):
                date, _, level = item.stem.rpartition("_ban")
                payload = json.loads(item.read_text(encoding="utf-8"))
                info = payload.get("info", []) if isinstance(payload, dict) else []
                raw_rows = info[0] if info and isinstance(info[0], list) else []
                for raw in raw_rows:
                    if isinstance(raw, list) and raw:
                        rows.append({"date": date, "symbol": str(raw[0]).zfill(6),
                                     "code": str(raw[0]).zfill(6),
                                     "name": str(raw[1]) if len(raw) > 1 else None,
                                     "plate_label": raw[5] if len(raw) > 5 else None,
                                     "theme_text": raw[12] if len(raw) > 12 else None,
                                     "board_height": raw[15] if len(raw) > 15 else None,
                                     "theme_codes": raw[19] if len(raw) > 19 else None,
                                     "lb_days": int(level) if level.isdigit() else None, "raw": raw})
            return rows
        if dataset == "hot_plates":
            rows = []
            for item in sorted(path.glob("hot_plates/*.json")):
                payload = json.loads(item.read_text(encoding="utf-8"))
                captured = payload.get("captured_local") or payload.get("available_at")
                if not captured and payload.get("Time"):
                    captured = datetime.fromtimestamp(float(payload["Time"]),
                        tz=ZoneInfo("Asia/Shanghai")).isoformat()
                response = payload.get("response", payload)
                for raw in response.get("list", []) if isinstance(response, dict) else []:
                    if isinstance(raw, list) and len(raw) >= 9:
                        rows.append({"date": str(captured or "")[:10], "available_at": captured,
                                     "code": str(raw[0]), "name": str(raw[1]),
                                     "strength": raw[2], "change_pct": raw[3],
                                     "amount_yuan": raw[5], "net_yuan": raw[6], "raw": raw})
            return rows
        raise ValueError(f"directory_input_requires_supported_dataset:{dataset}")
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]
    if path.suffix.lower() in {".jsonl", ".ndjson"}:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return _json_rows(path)


def _digest(path: Path) -> str:
    if path.is_dir():
        raw = b"".join(item.relative_to(path).as_posix().encode("utf-8") + b"\0" +
                       item.read_bytes() + b"\0"
                       for item in sorted(path.rglob("*")) if item.is_file())
    else:
        raw = path.read_bytes()
    return hashlib.sha256(raw).hexdigest()


def _stock_static_rows(names_path: Path, caps_path: Path, f10_path: Path | None,
                       *, pit_date: str | None) -> list[dict[str, Any]]:
    names = json.loads(names_path.read_text(encoding="utf-8"))
    caps = json.loads(caps_path.read_text(encoding="utf-8"))
    f10_rows = _rows(f10_path) if f10_path and f10_path.is_file() else []
    f10 = {str(row.get("股票代码", row.get("symbol", row.get("code", "")))).zfill(6): row
           for row in f10_rows}
    symbols = sorted({str(symbol).zfill(6) for symbol in names} | {str(symbol).zfill(6) for symbol in caps})
    rows = []
    for symbol in symbols:
        cap = caps.get(symbol, caps.get(symbol.lstrip("0"), {})) or {}
        detail = f10.get(symbol, {})
        limit_width = 20 if symbol.startswith(("300", "301", "688", "689")) else (
            30 if symbol.startswith(("4", "8", "92")) else 10)
        list_date = detail.get("新股上市日期") or detail.get("上市日期")
        list_date_text = str(list_date or "")
        if re.fullmatch(r"\d{8}", list_date_text):
            list_date_text = f"{list_date_text[:4]}-{list_date_text[4:6]}-{list_date_text[6:]}"
        try:
            listed_on = datetime.strptime(list_date_text[:10], "%Y-%m-%d").date()
            reference_day = datetime.strptime(str(pit_date), "%Y-%m-%d").date() if pit_date else None
            new_age_days = (reference_day - listed_on).days if reference_day else None
            new_stock = 0 <= new_age_days < 30 if new_age_days is not None else None
        except ValueError:
            new_stock = None
        f10_float = _number_or_none(detail.get("a股市值(不含限售股)"))
        f10_total = _number_or_none(detail.get("总市值"))
        rows.append({"symbol": symbol, "name": names.get(symbol),
                     "float_cap_yuan": f10_float if f10_float is not None else cap.get("float_cap"),
                     "total_cap_yuan": f10_total if f10_total is not None else cap.get("total_cap"),
                     "limit_width_pct": limit_width,
                     "is_st": detail.get("是否ST", detail.get("st")),
                     "is_new_stock": new_stock, "listing_date": list_date_text or None,
                     "industry": detail.get("所属同花顺行业"),
                     "pit_date": pit_date, "cap_as_of": "2026-10-14" if detail else "2026-10-13",
                     "source_quality": "PARTIAL" if not detail else "REFERENCE"})
    return rows


def _standard_rows(dataset: str, paths: list[Path], *, pit_date: str | None,
                   available_at: str) -> list[dict[str, Any]]:
    if dataset == "stock_static":
        if len(paths) < 2:
            raise ValueError("stock_static_requires_names_and_caps")
        return _stock_static_rows(paths[0], paths[1], paths[2] if len(paths) > 2 else None,
                                  pit_date=pit_date)
    if dataset == "plate_registry":
        path = paths[0]
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows = payload.get("plates", []) if isinstance(payload, dict) else payload
        generic_path = paths[1] if len(paths) > 1 else (
            Path(__file__).parent / "data" / "plate_generic_factor_names.json")
        generic_names = set(json.loads(generic_path.read_text(encoding="utf-8")).get("names", [])) if generic_path.is_file() else set()
        return [{**dict(row), "plate_code": str(row.get("plate_code", row.get("code", ""))),
                 "generic_factor": (row.get("name") in generic_names or
                                    row.get("class") == "GENERIC_FACTOR"), "pit_date": pit_date}
                for row in rows if isinstance(row, dict)]
    if dataset == "plate_members":
        rows = _rows(paths[0])
        return [{"plate_code": str(row.get("plate_code", row.get("plateid", row.get("code", "")))),
                 "symbol": str(row.get("symbol", row.get("stockid", row.get("code", "")))).zfill(6),
                 "pit_date": pit_date, "available_at": available_at}
                for row in rows]
    if dataset == "auction_baseline":
        rows = _rows(paths[0])
        return [{**row, "date": str(row.get("date", ""))[:10],
                 "sum_yi": _number_or_none(row.get("sum_yi")),
                 "threshold_wan": _number_or_none(row.get("threshold_wan")),
                 "available_at": available_at, "source_quality": "REFERENCE_ARCHIVE"}
                for row in rows]
    if dataset == "path_stats":
        rows = _rows(paths[0])
        return [{**row, "date": str(row.get("date", ""))[:10], "available_at": available_at,
                 "provenance": "research", "source_quality": "RESEARCH_DERIVED"}
                for row in rows]
    return _rows(paths[0], dataset)


def _number_or_none(value: Any) -> float | None:
    try:
        number = float(value) if value not in (None, "") else None
        return number if number is not None and math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def import_reference_sources(sources: dict[str, list[Path]], *, catalog: ArtifactCatalog,
                             trade_date: str, as_of: str, pit_date: str | None,
                             execute: bool = False,
                             available_at_by_dataset: dict[str, str] | None = None,
                             trade_date_by_dataset: dict[str, str] | None = None,
                             pit_date_by_dataset: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Normalize reference inputs and publish one immutable version per dataset."""
    output = []
    for dataset, paths in sources.items():
        if not paths or any(not path.exists() for path in paths):
            raise FileNotFoundError(f"reference_input_missing:{dataset}")
        dataset_pit_date = (pit_date_by_dataset or {}).get(dataset, pit_date)
        rows = _standard_rows(dataset, paths, pit_date=dataset_pit_date,
                              available_at=(available_at_by_dataset or {}).get(dataset, as_of))
        source_hashes = {str(path): _digest(path) for path in paths}
        for row in rows:
            row.setdefault("available_at", (available_at_by_dataset or {}).get(dataset, as_of))
            row.setdefault("pit_date", dataset_pit_date)
        if not rows:
            raise ValueError(f"empty_reference_dataset:{dataset}")
        aggregate = hashlib.sha256("".join(source_hashes.values()).encode()).hexdigest()
        artifact_date = (trade_date_by_dataset or {}).get(dataset, trade_date)
        manifest = {"trade_date": artifact_date, "as_of": as_of, "available_at":
                    (available_at_by_dataset or {}).get(dataset, as_of),
                    "point_in_time_date": dataset_pit_date, "provenance":
                    "research" if dataset == "path_stats" else "import_from_validation",
                    "source_sha256": aggregate, "source_sha256_by_path": source_hashes,
                    "source_path": [str(path.resolve()) for path in paths],
                    "pit_note": "STATIC_ASOF_UNKNOWN" if dataset == "plate_members" and not dataset_pit_date else None,
                    "row_count": len(rows), "status": "READY",
                    "quality": "RESEARCH_DERIVED" if dataset == "path_stats" else "REFERENCE"}
        written = catalog.commit(dataset, artifact_date, rows, manifest,
                                 source="import_from_validation") if execute else None
        output.append({"dataset": dataset, "path": [str(path.resolve()) for path in paths],
                       "rows": len(rows), "pit_date": dataset_pit_date,
                       "available_at": (available_at_by_dataset or {}).get(dataset, as_of),
                       "date_min": min((str(row.get("date") or row.get("event_date") or row.get("pit_date") or "")[:10]
                                                               for row in rows), default=None),
                       "date_max": max((str(row.get("date") or row.get("event_date") or row.get("pit_date") or "")[:10]
                                                               for row in rows), default=None),
                       "trade_date": artifact_date,
                       "source_sha256": aggregate, "source_sha256_by_path": source_hashes,
                       "execute": execute, "version": written.get("version") if written else None})
    return output


def _capture_time(directory: Path) -> str:
    match = re.search(r"(20\d{2}-\d{2}-\d{2})[-_T]?(\d{6})", directory.name)
    if match:
        return f"{match.group(1)}T{match.group(2)[:2]}:{match.group(2)[2:4]}:{match.group(2)[4:]}+08:00"
    manifest_path = directory / "manifest.json"
    if manifest_path.is_file():
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        value = payload.get("ended_at_utc") or payload.get("started_at_utc")
        if value:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.astimezone(ZoneInfo("Asia/Shanghai")).isoformat()
    raise ValueError(f"capture_time_unavailable:{directory}")


def import_kaipan_history(directories: list[Path], *, catalog: ArtifactCatalog,
                          execute: bool = False) -> list[dict[str, Any]]:
    """Import every captured daily snapshot with its actual capture availability."""
    output = []
    for directory in sorted(set(directories)):
        captured_at = _capture_time(directory)
        capture_day = captured_at[:10]
        pool_files = sorted(directory.glob("pools/*_ban*.json"))
        pool_rows = _rows(directory, "limit_up_pool") if pool_files else []
        event_days = sorted({item.stem.split("_ban", 1)[0] for item in pool_files})
        for event_day in event_days:
            rows = [row for row in pool_rows if row.get("date") == event_day]
            if not rows:
                output.append({"dataset": "limit_up_pool", "trade_date": event_day,
                               "available_at": captured_at, "rows": 0, "status": "EMPTY_SKIPPED"})
                continue
            source_files = [item for item in pool_files
                            if item.stem.split("_ban", 1)[0] == event_day]
            digest = hashlib.sha256("".join(_digest(item) for item in source_files).encode()).hexdigest()
            rows = [dict(row, available_at=captured_at, source_quality="KAIPAN_CAPTURE") for row in rows]
            manifest = {"trade_date": event_day, "as_of": captured_at, "available_at": captured_at,
                        "point_in_time_date": event_day, "provenance": "import_from_validation",
                        "source_sha256": digest, "source_path": [str(path) for path in source_files],
                        "row_count": len(rows),
                        "status": "READY", "capture_date": capture_day}
            committed = catalog.commit("limit_up_pool", event_day, rows, manifest,
                                       source="import_from_validation") if execute else None
            output.append({"dataset": "limit_up_pool", "trade_date": event_day,
                           "available_at": captured_at, "rows": len(rows),
                           "date_min": event_day, "date_max": event_day,
                           "point_in_time_date": event_day,
                           "source_sha256": digest, "version": committed.get("version") if committed else None})
        reason_dir = directory / "reasons"
        if reason_dir.is_dir():
            rows = _rows(directory, "limit_reasons")
            if not rows:
                output.append({"dataset": "limit_reasons", "trade_date": capture_day,
                               "available_at": captured_at, "rows": 0, "status": "EMPTY_SKIPPED"})
                continue
            rows = [dict(row, available_at=captured_at, capture_date=capture_day,
                         event_date=row.get("event_date") or row.get("Date")) for row in rows]
            digest = _digest(reason_dir)
            manifest = {"trade_date": capture_day, "as_of": captured_at, "available_at": captured_at,
                        "point_in_time_date": capture_day, "provenance": "import_from_validation",
                        "source_sha256": digest, "source_path": str(reason_dir), "row_count": len(rows),
                        "status": "READY", "capture_date": capture_day}
            committed = catalog.commit("limit_reasons", capture_day, rows, manifest,
                                       source="import_from_validation") if execute else None
            output.append({"dataset": "limit_reasons", "trade_date": capture_day,
                           "available_at": captured_at, "rows": len(rows),
                           "date_min": min((str(row.get("event_date") or "")[:10] for row in rows), default=None),
                           "date_max": max((str(row.get("event_date") or "")[:10] for row in rows), default=None),
                           "point_in_time_date": capture_day,
                           "source_sha256": digest, "version": committed.get("version") if committed else None})
    return output


def standard_reference_sources(validation_root: Path) -> dict[str, list[Path]]:
    data = validation_root / "market-extremes-study-20261015" / "data"
    f10 = validation_root / "f10-refresh-20261014" / "f10_refreshed.csv"
    stock = [data / "stock_names_20261013.json", data / "stock_caps_kaipan_20261013.json"]
    if f10.is_file():
        stock.append(f10)
    return {
        "stock_static": stock,
        "plate_registry": [validation_root / "plate-registry-pit-20261013" / "registry.json",
                           Path(__file__).parent / "data" / "plate_generic_factor_names.json"],
        "plate_members": [validation_root / "plate-registry-pit-20261013" / "relation.csv"],
        "auction_baseline": [validation_root / "ref-20261016" / "auction_amount_by_day.csv"],
        "path_stats": [validation_root / "ladder-pre0945-study-20261015" / "paths_daily.csv"],
    }


def import_files(mappings: list[str], *, catalog: ArtifactCatalog, trade_date: str,
                 as_of: str, pit_date: str | None, execute: bool = False) -> list[dict[str, Any]]:
    output = []
    for mapping in mappings:
        dataset, separator, raw_path = mapping.partition("=")
        if not separator or not dataset or not raw_path:
            raise ValueError("input_must_be_dataset_equals_path")
        path = Path(raw_path).expanduser().resolve()
        digest = _digest(path)
        rows = _rows(path, dataset)
        for row in rows:
            row.setdefault("available_at", as_of)
            if pit_date:
                row.setdefault("date", pit_date)
                row.setdefault("pit_date", pit_date)
        manifest = {"trade_date": trade_date, "as_of": as_of, "available_at": as_of,
                    "point_in_time_date": pit_date, "provenance": "import_from_validation",
                    "source_sha256": digest, "source_path": str(path),
                    "row_count": len(rows), "status": "READY"}
        written = catalog.commit(dataset, trade_date, rows, manifest, source="import_from_validation") if execute else None
        output.append({"dataset": dataset, "path": str(path), "rows": len(rows),
                       "source_sha256": digest, "execute": execute,
                       "version": written.get("version") if written else None})
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", default=[], metavar="DATASET=PATH")
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--pit-date")
    parser.add_argument("--root", default="~/core-data")
    parser.add_argument("--execute", action="store_true", help="commit immutable artifacts; default is dry-run")
    parser.add_argument("--standard-references", action="store_true",
                        help="import the standard validation reference bundle and captured Kaipan history")
    parser.add_argument("--validation-root", default="/home/exedev/validation")
    args = parser.parse_args(argv)
    if not args.input and not args.standard_references:
        parser.error("provide --input or --standard-references")
    catalog = ArtifactCatalog(Path(args.root).expanduser())
    results = []
    if args.standard_references:
        root = Path(args.validation_root)
        results.extend(import_reference_sources(standard_reference_sources(root), catalog=catalog,
            trade_date=args.trade_date, as_of=args.as_of, pit_date=args.pit_date,
            execute=args.execute, available_at_by_dataset={
                "stock_static": "2026-10-14T00:00:00+08:00",
                "plate_registry": "2026-10-13T15:10:00+08:00",
                "plate_members": "2026-10-13T15:10:00+08:00",
                "auction_baseline": args.as_of, "path_stats": args.as_of,
            }, trade_date_by_dataset={
                "stock_static": "2026-10-14", "plate_registry": "2026-10-13",
                "plate_members": "2026-10-13", "auction_baseline": "2026-10-15",
                "path_stats": "2026-10-15",
            }, pit_date_by_dataset={
                "stock_static": "2026-10-13", "plate_registry": "2026-10-13",
                "plate_members": "2026-10-13", "auction_baseline": "2026-10-15",
                "path_stats": "2026-10-15",
            }))
        daily_dirs = [path for path in (root / "kaipan-daily").iterdir()
                      if path.is_dir() and (path / "manifest.json").is_file()]
        daily_dirs.extend(path for path in root.glob("kaipan-fetch-20261013-fullhist*")
                          if path.is_dir())
        results.extend(import_kaipan_history(daily_dirs, catalog=catalog, execute=args.execute))
    if args.input:
        results.extend(import_files(args.input, catalog=catalog, trade_date=args.trade_date,
                                    as_of=args.as_of, pit_date=args.pit_date, execute=args.execute))
    print(json.dumps({"mode": "execute" if args.execute else "dry-run", "root": str(catalog.root),
                      "datasets": results}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
