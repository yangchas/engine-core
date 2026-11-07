"""Point-in-time plate amount baselines at 09:25 and 09:45."""
from __future__ import annotations

import json
import math
import os
import statistics
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .data_pipeline.storage import ArtifactCatalog, canonical_json

DATASET = "plate_amount_baseline"
SCHEMA = "plate_amount_baseline_v1"
WINDOW_DAYS = 64
SLOTS = ("0925", "0945")

def _day(value: Any) -> str:
    raw = str(value or "")[:10]
    if len(raw) == 8 and raw.isdigit():
        raw = f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return date.fromisoformat(raw).isoformat()


def _symbol(value: Any) -> str:
    raw = str(value or "").strip()
    return raw.zfill(6) if raw.isdigit() and len(raw) <= 6 else raw


def _percentile(values: Sequence[int], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    low, high = math.floor(position), math.ceil(position)
    return float(ordered[low]) if low == high else ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def build_plate_amount_baseline(*, target_date: str,
                                membership_by_date: Mapping[str, Iterable[Mapping[str, Any]]],
                                observations: Iterable[Mapping[str, Any]],
                                candidate_dates: Iterable[str],
                                membership_versions: Mapping[str, str] | None = None,
                                available_at: str | None = None) -> dict[str, Any]:
    target = _day(target_date)
    dates = sorted({_day(item) for item in candidate_dates if _day(item) < target})[-WINDOW_DAYS:]
    membership: dict[str, dict[str, set[str]]] = {}
    plates: set[str] = set()
    for raw_day, rows in membership_by_date.items():
        day = _day(raw_day)
        by_plate: dict[str, set[str]] = {}
        for row in rows:
            code = str(row.get("plate_code", row.get("plateid", row.get("code_plate", row.get("plate", "")))) or "").strip()
            symbol = _symbol(row.get("symbol", row.get("stock_code", row.get("stockid", row.get("code", "")))))
            if code and symbol:
                by_plate.setdefault(code, set()).add(symbol)
        membership[day] = by_plate
        plates.update(by_plate)
    amounts: dict[tuple[str, str, str], int] = {}
    for row in observations:
        day = _day(row.get("trade_date", row.get("date")))
        slot = str(row.get("slot") or "")
        symbol = _symbol(row.get("symbol"))
        try:
            amount = int(row.get("amount_yuan"))
        except (TypeError, ValueError, OverflowError):
            continue
        if day in dates and slot in SLOTS and symbol and amount >= 0:
            amounts[(day, slot, symbol)] = amount
    result_rows: list[dict[str, Any]] = []
    for code in sorted(plates):
        for slot in SLOTS:
            values: list[tuple[str, int]] = []
            missing: list[str] = []
            for day in dates:
                members = membership.get(day, {}).get(code)
                if not members:
                    missing.append(day)
                    continue
                facts = [amounts.get((day, slot, symbol)) for symbol in members]
                if any(value is None for value in facts):
                    missing.append(day)
                    continue
                values.append((day, sum(int(value) for value in facts if value is not None)))
            vals = [value for _, value in values]
            result_rows.append({
                "row_kind": "plate_baseline", "trade_date": target, "plate_code": code,
                "slot": slot, "median_yuan": statistics.median(vals) if vals else None,
                "p25_yuan": _percentile(vals, .25), "p75_yuan": _percentile(vals, .75),
                "sample_days": len(vals), "amounts_yuan": vals,
                "dates_used": [day for day, _ in values], "missing_dates": missing,
                "membership_versions": {day: (membership_versions or {}).get(day) for day, _ in values},
                "available_at": available_at,
            })
    refs = ([{"dataset": dataset, "trade_date": day, "version": None}
             for dataset in ("auction_snapshot_v2", "stock_tick_v2") for day in dates] +
            [{"dataset": "plate_members", "trade_date": day, "version": version}
             for day, version in sorted((membership_versions or {}).items())])
    return {"input_refs": refs, "status": "READY" if result_rows and all(not row["missing_dates"] for row in result_rows) else "PARTIAL",
            "target_date": target, "window_dates": dates, "rows": result_rows}


def _membership_inputs(catalog: ArtifactCatalog, dates: Sequence[str]):
    payload: dict[str, list[dict[str, Any]]] = {}
    versions: dict[str, str] = {}
    for day in dates:
        path = catalog.locate("plate_members", day)
        if not path:
            continue
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            payload[day] = catalog.read_rows(path)
            versions[day] = str(manifest.get("version") or "unknown")
        except (OSError, ValueError, TypeError):
            continue
    return payload, versions


def _existing(catalog: ArtifactCatalog, target: str):
    path = catalog.locate(DATASET, target)
    if not path:
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        catalog_versions = catalog.read().get("datasets", {}).get(DATASET, {}).get("dates", {}).get(target, [])
        if manifest.get("schema") == SCHEMA and any(v.get("version") == manifest.get("version") for v in catalog_versions):
            return manifest
    except (OSError, ValueError, TypeError):
        pass
    return None


def _publish(target: str, manifest: Mapping[str, Any], rows: Sequence[Mapping[str, Any]],
             current_codes: set[str]) -> bool:
    url = os.getenv("CORE_REDIS_URL") or os.getenv("REDIS_URL")
    if not url:
        return False
    import redis
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        code = str(row.get("plate_code") or "")
        if code in current_codes:
            grouped.setdefault(code, []).append({k: row.get(k) for k in
                ("plate_code", "slot", "median_yuan", "sample_days", "dates_used", "missing_dates")})
    client = redis.Redis.from_url(url, decode_responses=True)
    for code, compact in grouped.items():
        payload = canonical_json({"dataset": DATASET, "trade_date": target,
                                  "version": manifest.get("version"), "rows": compact})
        client.set(f"core:cache:data:{DATASET}:{target}:{code}", payload.decode(), ex=30 * 86400)
    return True


def build_and_store_plate_amount_baseline(target_date: str, *, artifact_root: str | Path,
                                          publish: bool = True, available_at: str | None = None,
                                          pull_upstream: bool = False) -> dict[str, Any]:
    from .ladder_daily_adapter import trading_days
    from .data_pipeline import auction_amount_facts
    from .data_pipeline.td_sink import TaosStore
    target = _day(target_date)
    catalog = ArtifactCatalog(artifact_root)
    old = _existing(catalog, target)
    if old and old.get("business_status") == "READY":
        current_rows = catalog.read_rows(catalog.locate("plate_members", target)) if catalog.locate("plate_members", target) else []
        current_codes = {str(row.get("plate_code") or row.get("plateid") or "") for row in current_rows}
        return {"status": old.get("business_status", "PARTIAL"), "reused": True,
                "version": old.get("version"), "row_count": old.get("row_count"),
                "redis_published": _publish(target, old, catalog.read_rows(Path(old["manifest_uri"])) if old.get("manifest_uri") else [], current_codes) if publish else False}
    dates = [day for day in trading_days(Path(artifact_root), target) if day < target][-WINDOW_DAYS:]
    facts = auction_amount_facts.AuctionAmountFacts(TaosStore('core_data'))
    if pull_upstream and dates:
        code, reply = auction_amount_facts.run({'days': dates}, artifact_root, store=facts)
        if code not in (0, 10, 20):
            return {'status': 'UPSTREAM_FAILED', 'error_code': reply['error_code'], 'detail': reply['detail']}
    complete = {slot: facts.complete_days(dates, slot) for slot in SLOTS}
    missing = [{'dataset': 'auction_amount' if slot == '0925' else 'slot0945_amount', 'trade_date': day}
               for slot in SLOTS for day in dates if day not in complete[slot]]
    candidates = sorted(complete['0925'] & complete['0945'])
    if not candidates:
        return {'status': 'NOT_YET_AVAILABLE', 'missing': missing}
    observations = []
    for slot in SLOTS:
        rows_by_day = facts.read_days(candidates, slot)
        for day in candidates:
            if day not in rows_by_day:
                raise RuntimeError(f'AUCTION_FACTS_CHANGED_DURING_READ:{day}:{slot}')
        for day, amounts in rows_by_day.items():
            observations.extend({'trade_date': day, 'slot': slot, 'symbol': symbol, 'amount_yuan': amount}
                                for symbol, amount in amounts.items())
    membership_by_date, versions = _membership_inputs(catalog, candidates)
    available = available_at if available_at is not None else datetime.now(timezone.utc).isoformat()
    built = build_plate_amount_baseline(target_date=target, membership_by_date=membership_by_date,
        observations=observations, candidate_dates=candidates, membership_versions=versions, available_at=available)
    if missing or len(dates) < WINDOW_DAYS:
        built['status'] = 'PARTIAL'
    built['input_refs'] = sorted(
        [{'dataset': 'auction_amount' if slot == '0925' else 'slot0945_amount', 'trade_date': day, 'version': None}
         for slot in SLOTS for day in candidates] +
        [{'dataset': 'plate_members', 'trade_date': day, 'version': version} for day, version in versions.items()],
        key=lambda ref: (ref['dataset'], ref['trade_date']))
    manifest = {"schema": SCHEMA, "dataset": DATASET, "trade_date": target,
        "point_in_time_date": max(dates) if dates else None, "available_at": available,
        "source": ["auction_amount", "slot0945_amount", "plate_members"],
        "business_status": built["status"], "window": {"sessions": WINDOW_DAYS,
        "excludes_target_date": True, "dates": dates}, "membership_versions": versions,
        "input_refs": built["input_refs"]}
    committed = catalog.commit(DATASET, target, built["rows"], manifest, source="core_td_plate_amount")
    current_path = catalog.locate("plate_members", target)
    current_members = catalog.read_rows(current_path) if current_path else []
    current_codes = {str(row.get("plate_code") or row.get("plateid") or "") for row in current_members}
    published = _publish(target, committed, built["rows"], current_codes) if publish else False
    return {"status": built["status"], "reused": False, "version": committed["version"],
            "sha256": committed["sha256"], "row_count": committed["row_count"],
            "available_at": committed["available_at"], "date_count": len(dates),
            "membership_date_count": len(membership_by_date), "missing": missing,
            "redis_published": published}
