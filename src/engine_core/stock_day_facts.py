"""Compact, versioned daily stock facts assembled from explicit file inputs."""
from __future__ import annotations

import csv
import gzip
import hashlib
import json
import math
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = "StockDayFactsV1"
STAGES = {"auction", "open", "close"}
FACT_COLUMNS = """trade_date symbol schema_version revision universe_id input_manifest_sha256 name display_name name_source main_plate_code main_plate_name plate_list_summary membership_version membership_asof industry_fallback plate_codes limit_pct limit_up_milli limit_down_milli pre_close_milli yesterday_lb_days yesterday_limit_up yesterday_price_limit_proxy previous_down_streak_proxy is_new_listing is_suspended float_cap_yuan total_cap_yuan range20_low_milli range20_high_milli position20_0925_pct yesterday_pool_source yesterday_pool_st_policy limit_rule_version cap_basis adjustment_basis p0920_px_milli p0920_pct p0920_cumulative_amount_yuan p0920_price_status p0920_source_event_at p0920_available_at p0920_read_at p0920_source_id p0920_quality_bits p0924_px_milli p0924_pct p0924_cumulative_amount_yuan p0924_price_status p0924_source_event_at p0924_available_at p0924_read_at p0924_source_id p0924_quality_bits p0925_px_milli p0925_pct p0925_cumulative_amount_yuan p0925_price_status p0925_source_event_at p0925_available_at p0925_read_at p0925_source_id p0925_quality_bits p0930_px_milli p0930_pct p0930_cumulative_amount_yuan p0930_price_status p0930_source_event_at p0930_available_at p0930_read_at p0930_source_id p0930_quality_bits p0932_px_milli p0932_pct p0932_cumulative_amount_yuan p0932_price_status p0932_source_event_at p0932_available_at p0932_read_at p0932_source_id p0932_quality_bits p0935_px_milli p0935_pct p0935_cumulative_amount_yuan p0935_price_status p0935_source_event_at p0935_available_at p0935_read_at p0935_source_id p0935_quality_bits p0940_px_milli p0940_pct p0940_cumulative_amount_yuan p0940_price_status p0940_source_event_at p0940_available_at p0940_read_at p0940_source_id p0940_quality_bits p0945_px_milli p0945_pct p0945_cumulative_amount_yuan p0945_price_status p0945_source_event_at p0945_available_at p0945_read_at p0945_source_id p0945_quality_bits auction_match_amount_yuan auction_market_share_bp auction_rest_bid_yuan auction_rest_ask_yuan auction_limit_state auction_seal_observed window_0925_0945_high_milli window_0925_0945_low_milli day_open_milli day_high_milli day_low_milli day_close_milli day_volume_shares day_amount_yuan day_pct hit_up_price_proxy hit_down_price_proxy close_up_price_proxy close_down_price_proxy close_seal_up close_seal_down delta_close_auction_pp delta_over_limit auction_amount_rank auction_vs_5d_same_time day_amount_vs_prev quality_bits missing_reason_codes field_provenance_ref computed_at""".split()
_BOOL_FIELDS = {"yesterday_limit_up", "yesterday_price_limit_proxy", "is_new_listing", "is_suspended", "auction_seal_observed", "hit_up_price_proxy", "hit_down_price_proxy", "close_up_price_proxy", "close_down_price_proxy", "close_seal_up", "close_seal_down"}
_LIST_FIELDS = {"plate_codes", "plate_list_summary", "quality_bits", "missing_reason_codes"} | {f"p{t}_{s}" for t in ("0920", "0924", "0925", "0930", "0932", "0935", "0940", "0945") for s in ("quality_bits",)}
_INT_FIELDS = {"revision", "yesterday_lb_days", "previous_down_streak_proxy", "limit_up_milli", "limit_down_milli", "pre_close_milli", "range20_low_milli", "range20_high_milli", "auction_limit_state", "window_0925_0945_high_milli", "window_0925_0945_low_milli", "day_open_milli", "day_high_milli", "day_low_milli", "day_close_milli", "day_volume_shares"} | {f"p{t}_{s}" for t in ("0920", "0924", "0925", "0930", "0932", "0935", "0940", "0945") for s in ("px_milli",)}
_FLOAT_FIELDS = {"limit_pct", "position20_0925_pct", "day_pct", "delta_close_auction_pp", "delta_over_limit", "auction_amount_rank", "auction_vs_5d_same_time", "day_amount_vs_prev", "auction_market_share_bp"} | {f"p{t}_{s}" for t in ("0920", "0924", "0925", "0930", "0932", "0935", "0940", "0945") for s in ("pct",)}


def _symbol(row: Mapping[str, Any]) -> str:
    value = row.get("symbol", row.get("code", row.get("stockid")))
    return str(value or "").strip().zfill(6) if str(value or "").strip() else ""


def _number(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _first(row: Mapping[str, Any], *keys: str) -> Any:
    return next((row[k] for k in keys if k in row and row[k] not in (None, "")), None)


def _source_hash(inputs: Mapping[str, Sequence[Mapping[str, Any]]]) -> str:
    payload = json.dumps(inputs, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _pct(row: Mapping[str, Any]) -> float | None:
    bp = _number(row.get("chg_bp"))
    if bp is not None:
        return bp / 100.0
    px, pre = _number(row.get("px")), _number(row.get("pre_close"))
    if px is None:
        milli = _number(row.get("px_milli"))
        px = milli / 1000 if milli is not None else None
    if pre is None:
        milli = _number(row.get("pre_close_milli"))
        pre = milli / 1000 if milli is not None else None
    return (px / pre - 1) * 100 if px is not None and pre else None


def _millis(value: Any, *, already_milli: bool = False) -> int | None:
    number = _number(value)
    if number is None:
        return None
    return int(round(number if already_milli else number * 1000))


def _records(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        nested = value.get("stocks", value.get("rows"))
        if nested is not None:
            value = nested
        else:
            value = [dict(row, symbol=key) if isinstance(row, Mapping) else row
                     for key, row in value.items()]
    return [r for r in (value or []) if isinstance(r, Mapping)]


def build_facts(inputs: Mapping[str, Any], *, trade_date: str, stage: str,
                previous_rows: Sequence[Mapping[str, Any]] | None = None,
                universe_id: str = "EXPLICIT_INPUT_UNION") -> list[dict[str, Any]]:
    """Build one row per symbol. Inputs are named row collections; no service access occurs."""
    if stage not in STAGES:
        raise ValueError(f"stage must be one of {sorted(STAGES)}")
    source_rows = {str(k): _records(v) for k, v in inputs.items()}
    # A later stage is a distinct durable revision even when its source rows
    # happen to hash identically (for example, a day with no auction updates).
    fingerprint = _source_hash({"stage": stage, **source_rows})
    merged: dict[str, dict[str, Any]] = {}
    for row in previous_rows or ():
        symbol = _symbol(row)
        if symbol:
            merged[symbol] = {name: row.get(name) for name in FACT_COLUMNS}
    maps: dict[str, dict[str, Mapping[str, Any]]] = {}
    for key, rows in source_rows.items():
        by_symbol: dict[str, Mapping[str, Any]] = {}
        for row in rows:
            symbol = _symbol(row)
            if symbol:
                if key == "relations" and symbol in by_symbol:
                    old = dict(by_symbol[symbol])
                    prior = old.get("plate_codes", old.get("plates", []))
                    if not isinstance(prior, list):
                        prior = [prior] if prior else []
                    code = _first(row, "plate_code", "plateid")
                    name = _first(row, "plate_name", "name")
                    prior_names = old.get("plate_list_summary", [])
                    if not isinstance(prior_names, list):
                        prior_names = [prior_names] if prior_names else []
                    if code:
                        prior.append(str(code))
                    if name:
                        prior_names.append(str(name))
                    old["plate_codes"] = list(dict.fromkeys(prior))
                    old["plate_list_summary"] = list(dict.fromkeys(prior_names))
                    if old["plate_list_summary"]:
                        old["main_plate_name"] = old["plate_list_summary"][0]
                    by_symbol[symbol] = old
                elif key == "relations":
                    rel = dict(row)
                    code = _first(row, "plate_code", "plateid")
                    name = _first(row, "plate_name", "name")
                    if code:
                        rel["plate_codes"] = [str(code)]
                    if name:
                        rel["plate_list_summary"] = [str(name)]
                        rel["main_plate_name"] = str(name)
                    by_symbol[symbol] = rel
                else:
                    by_symbol[symbol] = row
        maps[key] = by_symbol
    checkpoint_by_tag: dict[str, dict[str, Mapping[str, Any]]] = {}
    prior_closes: dict[str, list[tuple[str, float]]] = {}
    for item in source_rows.get("daily_history", []):
        symbol = _symbol(item)
        day = str(_first(item, "date", "trade_date") or "")
        close = _number(_first(item, "close", "day_close"))
        if symbol and day < trade_date and close is not None:
            prior_closes.setdefault(symbol, []).append((day, close))
    for point in source_rows.get("checkpoints", []):
        symbol, tag = _symbol(point), str(point.get("tag") or "")
        if symbol and tag:
            checkpoint_by_tag.setdefault(tag, {})[symbol] = point
    auction_by_tag: dict[str, dict[str, Mapping[str, Any]]] = {}
    for point in source_rows.get("auction", []):
        symbol, tag = _symbol(point), str(_first(point, "auction_tag", "tag") or "")
        if symbol and tag:
            auction_by_tag.setdefault(tag, {})[symbol] = point
    symbols = set(merged)
    for by_symbol in maps.values():
        symbols.update(by_symbol)
    now = datetime.now(timezone.utc).isoformat()
    name_sources = [maps.get(k, {}) for k in ("names", "stocks", "security_master", "daily")]
    for symbol in sorted(symbols):
        row = merged.setdefault(symbol, {c: None for c in FACT_COLUMNS})
        prior_fp = row.get("input_manifest_sha256")
        row.update(trade_date=trade_date, symbol=symbol, schema_version=SCHEMA_VERSION,
                   revision=(int(row.get("revision") or 0) + (prior_fp != fingerprint)),
                   universe_id=universe_id, input_manifest_sha256=fingerprint,
                   computed_at=now, field_provenance_ref=fingerprint)
        for names in name_sources:
            candidate = names.get(symbol)
            if candidate and _first(candidate, "name", "stock_name", "证券名称"):
                row["name"] = str(_first(candidate, "name", "stock_name", "证券名称"))
                row["name_source"] = next(k for k in ("names", "stocks", "security_master", "daily") if maps.get(k) is names)
                break
        row["display_name"] = row.get("name") or "名称未知"
        qualities = list(row.get("quality_bits") or [])
        missing = list(row.get("missing_reason_codes") or [])
        if not row.get("name"):
            qualities.append("NAME_MISSING")
            missing.append("MISSING_NAME")
        else:
            qualities = [x for x in qualities if x != "NAME_MISSING"]
            missing = [x for x in missing if x != "MISSING_NAME"]
        stock = maps.get("stocks", {}).get(symbol, {})
        pool = maps.get("yesterday_pool", {}).get(symbol, {})
        relation = maps.get("relations", {}).get(symbol, {})
        caps = maps.get("caps", {}).get(symbol, {})
        if stock:
            row["limit_pct"] = _number(_first(stock, "limit_pct", "limit_percent")) if _first(stock, "limit_pct", "limit_percent") is not None else row.get("limit_pct")
            row["pre_close_milli"] = _millis(_first(stock, "pre_close", "pre_close_milli"), already_milli="pre_close_milli" in stock) if _first(stock, "pre_close", "pre_close_milli") is not None else row.get("pre_close_milli")
            row["industry_fallback"] = _first(stock, "industry", "industry_fallback") or row.get("industry_fallback")
        if row.get("limit_pct") is None:
            from .extremes_review import limit_percent
            row["limit_pct"] = limit_percent(symbol, str(row.get("name") or ""))
        if "yesterday_pool" in maps:
            row["yesterday_lb_days"] = _number(_first(pool, "lb_days", "streak", "prior_streak")) if pool else None
            row["yesterday_limit_up"] = bool(pool)
            row["yesterday_pool_source"] = "DATED_POOL"
        if relation:
            row["main_plate_name"] = _first(relation, "main_plate_name", "main_plate") or row.get("main_plate_name")
            row["plate_codes"] = _first(relation, "plate_codes", "plates") or row.get("plate_codes")
            row["plate_list_summary"] = _first(relation, "plate_list_summary") or row.get("plate_list_summary")
        if caps:
            row["float_cap_yuan"] = _number(_first(caps, "float_cap_yuan", "float_cap")) if _first(caps, "float_cap_yuan", "float_cap") is not None else row.get("float_cap_yuan")
            row["total_cap_yuan"] = _number(_first(caps, "total_cap_yuan", "total_cap")) if _first(caps, "total_cap_yuan", "total_cap") is not None else row.get("total_cap_yuan")
        for tag in ("0920", "0924", "0925", "0930", "0932", "0935", "0940", "0945"):
            point = checkpoint_by_tag.get(tag, {}).get(symbol)
            auction = auction_by_tag.get(tag, {}).get(symbol)
            if stage == "auction" and tag not in ("0920", "0924", "0925"):
                continue
            if stage == "open" and tag in ("0920", "0924", "0925") and not auction and not point:
                continue
            if stage == "open" and tag not in ("0920", "0924", "0925", "0930", "0932", "0935", "0940", "0945"):
                continue
            if stage == "close":
                continue
            # Tick checkpoints carry the best price coverage at 0920/0924/0925;
            # auction snapshots remain authoritative for matched amounts.
            chosen = ((auction if auction and _pct(auction) is not None else point or auction)
                      if tag == "0925" else
                      (point if point and _pct(point) is not None else auction or point)) or {}
            pct = _pct(chosen)
            if pct is not None:
                row[f"p{tag}_pct"] = pct
            px_is_milli = "px_milli" in chosen or (not auction and tag in str(chosen.get("tag", "")) and chosen.get("px") is not None)
            px_milli = _millis(chosen.get("px_milli"), already_milli=True) if "px_milli" in chosen else _millis(chosen.get("px"), already_milli=px_is_milli)
            if px_milli is not None:
                row[f"p{tag}_px_milli"] = px_milli
            amount_source = auction if auction and _first(auction, "match_amt_yuan", "amt_yuan_cum", "cumulative_amount_yuan") is not None else chosen
            amount = _number(_first(amount_source, "match_amt_yuan", "amt_yuan_cum", "cumulative_amount_yuan"))
            if amount is not None:
                row[f"p{tag}_cumulative_amount_yuan"] = amount
            if chosen:
                explicit_status = _first(chosen, "price_status", "auction_price_status")
                tick_time = str(_first(chosen, "last_tick_ts", "event_at", "ts") or "")
                if explicit_status:
                    price_status = explicit_status
                elif tag != "0925":
                    price_status = "INDICATIVE"
                elif chosen is point and tick_time:
                    price_status = "FINAL" if tick_time >= "09:25:00" else "INDICATIVE"
                elif auction and _pct(auction) is not None:
                    price_status = "FINAL"
                else:
                    price_status = "UNKNOWN"
                row[f"p{tag}_price_status"] = price_status
                row[f"p{tag}_source_event_at"] = _first(chosen, "ts", "last_tick_ts", "event_at")
                row[f"p{tag}_available_at"] = _first(chosen, "available_at")
                row[f"p{tag}_read_at"] = _first(chosen, "read_at")
                row[f"p{tag}_source_id"] = "checkpoint_tick" if chosen is point else "auction_snapshot"
                if not row.get("pre_close_milli"):
                    pre_value = _first(chosen, "pre_close_milli", "pre_close")
                    if pre_value is not None:
                        row["pre_close_milli"] = _millis(
                            pre_value, already_milli="pre_close_milli" in chosen or chosen is point)
            if point and tag == "0945":
                row["window_0925_0945_high_milli"] = _millis(point.get("high_px"), already_milli="high_px_milli" in point) or row.get("window_0925_0945_high_milli")
                row["window_0925_0945_low_milli"] = _millis(point.get("low_px"), already_milli="low_px_milli" in point) or row.get("window_0925_0945_low_milli")
        a25 = auction_by_tag.get("0925", {}).get(symbol, {})
        if a25:
            row["auction_match_amount_yuan"] = _number(_first(a25, "match_amt_yuan", "auction_match_amount_yuan"))
            row["auction_rest_bid_yuan"] = _number(_first(a25, "rest_bid_amt_yuan", "rest_bid_yuan"))
            row["auction_rest_ask_yuan"] = _number(_first(a25, "rest_ask_amt_yuan", "rest_ask_yuan"))
            row["auction_limit_state"] = _number(a25.get("limit_state"))
            row["auction_seal_observed"] = bool(_number(row["auction_limit_state"]) == 1 and row["auction_rest_bid_yuan"] and row["auction_rest_bid_yuan"] > 0)
        if stage == "close":
            day = maps.get("daily", {}).get(symbol, {})
            if day:
                if not row.get("pre_close_milli"):
                    daily_pre = _first(day, "pre_close_milli", "pre_close")
                    if daily_pre is not None:
                        row["pre_close_milli"] = _millis(daily_pre, already_milli="pre_close_milli" in day)
                for field, candidates in {"day_open_milli": ("open", "day_open_milli"), "day_high_milli": ("high", "day_high_milli"), "day_low_milli": ("low", "day_low_milli"), "day_close_milli": ("close", "day_close_milli")}.items():
                    raw = _first(day, *candidates)
                    if raw is not None:
                        row[field] = _millis(raw, already_milli=field in day)
                # The captured daily-kline contract calls this field `volume`.
                # Keep the unit explicit in the wide-table column name.
                row["day_volume_shares"] = _number(_first(day, "volume_shares", "day_volume_shares", "volume"))
                row["day_amount_yuan"] = _number(_first(day, "amount_yuan", "day_amount_yuan"))
                row["day_pct"] = _number(_first(day, "pct_chg", "day_pct"))
                if row["day_pct"] is None:
                    row["day_pct"] = _pct({"px": _first(day, "close"), "pre_close": _first(day, "pre_close")})
                if row.get("day_pct") is not None and row.get("p0925_pct") is not None:
                    row["delta_close_auction_pp"] = row["day_pct"] - row["p0925_pct"]
            history20 = sorted(prior_closes.get(symbol, []))[-20:]
            if len(history20) == 20:
                values = [value for _, value in history20]
                row["range20_low_milli"] = _millis(min(values))
                row["range20_high_milli"] = _millis(max(values))
                auction_px = row.get("p0925_px_milli")
                if auction_px is None and row.get("pre_close_milli") and row.get("p0925_pct") is not None:
                    auction_px = row["pre_close_milli"] * (1 + row["p0925_pct"] / 100)
                if auction_px is not None and max(values) > min(values):
                    row["position20_0925_pct"] = (auction_px / 1000 - min(values)) / (max(values) - min(values)) * 100
        if stage == "close":
            daily_row = maps.get("daily", {}).get(symbol, {})
            if not daily_row:
                missing.append("MISSING_DAILY_KLINE")
            else:
                missing = [value for value in missing if value != "MISSING_DAILY_KLINE"]
                if row.get("day_close_milli") is None or row.get("day_pct") is None:
                    missing.append("INCOMPLETE_DAILY_KLINE")
                else:
                    missing = [value for value in missing if value != "INCOMPLETE_DAILY_KLINE"]
        pool = maps.get("yesterday_pool", {}).get(symbol, {})
        if row.get("yesterday_limit_up") and not _first(pool, "lb_days", "streak", "prior_streak"):
            missing.append("MISSING_YESTERDAY_LB_DAYS")
        else:
            missing = [value for value in missing if value != "MISSING_YESTERDAY_LB_DAYS"]
        row["quality_bits"] = sorted(set(qualities))
        row["missing_reason_codes"] = sorted(set(missing))
    return [merged[s] for s in sorted(merged)]


def write_facts(rows: Sequence[Mapping[str, Any]], out_dir: str | Path, *, trade_date: str,
                stage: str, source_manifest_sha256: str | None = None) -> dict[str, Any]:
    if stage not in STAGES:
        raise ValueError(f"stage must be one of {sorted(STAGES)}")
    started = time.perf_counter()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    material = [{k: r.get(k) for k in FACT_COLUMNS} for r in rows]
    revision = max((int(r.get("revision") or 1) for r in material), default=1)
    final = out / f"stock_day_facts-{trade_date}-r{revision}.csv.gz"
    fd, tmp_name = tempfile.mkstemp(prefix=".stock-day-facts-", suffix=".tmp", dir=out)
    os.close(fd)
    try:
        with gzip.open(tmp_name, "wt", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FACT_COLUMNS)
            writer.writeheader()
            for row in material:
                writer.writerow({k: json.dumps(row.get(k), ensure_ascii=False, separators=(",", ":")) if k in _LIST_FIELDS and row.get(k) is not None else row.get(k) for k in FACT_COLUMNS})
        os.replace(tmp_name, final)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    digest = hashlib.sha256(final.read_bytes()).hexdigest()
    manifest = {"contract": SCHEMA_VERSION, "trade_date": trade_date, "stage": stage,
                "revision": revision, "row_count": len(material), "sha256": digest,
                "source_manifest_sha256": source_manifest_sha256,
                "coverage": {field: sum(r.get(field) is not None for r in material) for field in ("name", "p0925_pct", "p0945_pct", "day_pct")},
                "build_seconds": round(time.perf_counter() - started, 6), "computed_at": datetime.now(timezone.utc).isoformat(),
                "format": "CSV.GZ"}
    manifest_tmp = out / ".manifest.tmp"
    manifest_tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(manifest_tmp, out / "manifest.json")
    return manifest


def _parse(value: str, field: str) -> Any:
    if value == "":
        return None
    if field in _LIST_FIELDS:
        try: return json.loads(value)
        except ValueError: return []
    if field in _BOOL_FIELDS:
        return value.lower() in ("true", "1")
    if field in _INT_FIELDS:
        try: return int(float(value))
        except ValueError: return None
    if field in _FLOAT_FIELDS or field.endswith("_yuan") or field.endswith("_cap_yuan"):
        try: return float(value)
        except ValueError: return None
    return value


def read_facts(path: str | Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    base = Path(path)
    if base.is_dir():
        manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
        data_path = base / f"stock_day_facts-{manifest['trade_date']}-r{manifest['revision']}.csv.gz"
    else:
        data_path = base
        manifest = json.loads((base.parent / "manifest.json").read_text(encoding="utf-8"))
    with gzip.open(data_path, "rt", encoding="utf-8", newline="") as f:
        rows = [{k: _parse(v, k) for k, v in row.items()} for row in csv.DictReader(f)]
    return rows, manifest

