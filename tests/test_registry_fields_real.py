from __future__ import annotations

import ast
import gzip
import json
import re
from pathlib import Path

from engine_core.data_pipeline.registry import DATASETS


REAL = Path(__file__).parent / "fixtures" / "real" / "c526"
REAL_C527 = Path(__file__).parent / "fixtures" / "real" / "c527"
UNKNOWN = "UNKNOWN"
ARTIFACT_ONLY_SOURCE_FIELDS = {
    ("daily_kline", "baostock"): {"code", "mismatch", "turnover", "universe_version"},
}
ALIASES = {
    "quote_slice": {
        "redis_q2": {"px": "price_milli", "pc": "pre_close_milli", "amt": "amount_yuan",
                     "vol": "volume_shares", "ts": "source_ts", "ls": "limit_state",
                     "am": "match_amount_yuan", "br": "seal_buy_yuan", "ar": "seal_sell_yuan"},
        "td_auction_snapshot_v2": {"auction_tag": "tag", "match_amt_yuan": "match_amount_yuan",
                                   "chg_bp": "pct_bp", "px_milli": "price_milli",
                                   "rest_bid_amt_yuan": "seal_buy_yuan",
                                   "rest_ask_amt_yuan": "seal_sell_yuan", "ts": "source_ts"},
        "wencai_auction": {"match_price": "price_milli", "match_pct": "pct_bp",
                           "match_volume": "volume_shares", "amount_yuan": "match_amount_yuan"},
    }
}


def _json(name):
    return json.loads((REAL / name).read_text(encoding="utf-8"))


def _gzip_json(name):
    with gzip.open(REAL / name, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def _source_rows(dataset, source):
    if dataset == "daily_kline":
        filename = {
            "baostock": "daily_kline_baostock_first200.json",
            "wencai": "daily_kline_wencai_first200.json",
            "q2_final": "daily_kline_q2_final_first200.json.gz",
        }[source]
        capture = _gzip_json(filename) if filename.endswith(".gz") else _json(filename)
        return capture["rows"]
    if source == "redis_q2":
        hashes = _gzip_json("redis_q2_hashes_20261023_first60.json.gz")
        return [{**item["value"], "symbol": symbol} for symbol, item in hashes.items()]
    if source == "td_auction_snapshot_v2":
        with gzip.open(REAL_C527 / "td_auction_snapshot_v2_20261023_first70.json.gz",
                       "rt", encoding="utf-8") as stream:
            capture = json.load(stream)
        return [dict(zip(capture["cols"], row)) for row in capture["rows"]]
    filename = {
        "wencai_auction": "wencai_auction_fallback_first60.json.gz",
    }[source]
    if filename.endswith(".gz"):
        with gzip.open(REAL / filename, "rt", encoding="utf-8") as stream:
            return json.load(stream)["rows"]
    return _json(filename)["rows"]


def _source_key_rows(dataset, source):
    if dataset == "quote_slice" and source == "wencai_auction":
        with gzip.open(REAL_C527 / "wencai_auction_upstream_first60.json.gz",
                       "rt", encoding="utf-8") as stream:
            capture = json.load(stream)
        return capture["rows"]
    return _source_rows(dataset, source)


def _numeric(value):
    if isinstance(value, bool):
        raise ValueError("boolean is not numeric data")
    return float(value)


def _canonical_value(field, value, source):
    if value is None:
        return None
    unit = field.canonical_unit
    source_unit = field.source_units.get(source, UNKNOWN)
    if field.type is int and unit == "price_milli" and source_unit.startswith("yuan"):
        return round(_numeric(value) * 1000)
    if field.type is int and unit == "bp" and source_unit.startswith("percent"):
        return round(_numeric(value) * 100)
    if field.type is int and unit == "shares" and source_unit.startswith("hand"):
        return round(_numeric(value) * 100)
    if field.type is int and unit == "yuan":
        return round(_numeric(value))
    if field.type is int and isinstance(value, str):
        return round(float(value))
    if field.type is int and isinstance(value, (int, float)):
        return round(value)
    if field.type is float and isinstance(value, str):
        return float(value)
    return value


def _type_matches(value, expected):
    if expected is bool:
        return isinstance(value, bool)
    if expected is int:
        return isinstance(value, int) and not isinstance(value, bool)
    if expected is float:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, expected)


def _project_row(dataset, source, row, field_by_name):
    aliases = ALIASES.get(dataset, {}).get(source, {})
    projected = {}
    undeclared = []
    for raw_name, raw_value in row.items():
        field_name = aliases.get(raw_name, raw_name)
        field = field_by_name.get(field_name)
        if field is None:
            undeclared.append(raw_name)
            continue
        value = raw_value if field.raw_only else _canonical_value(field, raw_value, source)
        if value is None:
            assert field.nullable, f"{dataset}/{source}.{raw_name} is null but non-nullable"
        elif field.raw_only:
            pass
        else:
            source_unit = field.source_units.get(source, UNKNOWN)
            numeric_string_allowed = "string" in source_unit.lower() or "字符串" in field.description
            if isinstance(raw_value, str) and field.type in (int, float) and numeric_string_allowed:
                assert _type_matches(value, field.type), (dataset, source, raw_name, value, field.type)
            else:
                assert not (isinstance(raw_value, str) and field.type in (int, float)), (
                    dataset, source, raw_name, "numeric string lacks source-unit/description annotation")
                assert _type_matches(value, field.type), (dataset, source, raw_name, value, field.type)
        projected[field_name] = value
    assert not undeclared, f"undeclared fields in {dataset}/{source}: {sorted(set(undeclared))}"
    return projected


def _check_names(expression):
    return {node.id for node in ast.walk(ast.parse(expression, mode="eval"))
            if isinstance(node, ast.Name)} - {"abs", "round", "min", "max"}


def _daily_preclose_by_symbol():
    fields = {field.name: field for field in DATASETS["daily_kline"].fields}
    rows = _source_rows("daily_kline", "q2_final")
    result = {}
    for row in rows:
        normalized = _project_row("daily_kline", "q2_final", row, fields)
        if normalized.get("symbol") and normalized.get("pre_close") is not None:
            result[normalized["symbol"]] = normalized["pre_close"]
    return result


def _td_check_ratios():
    capture_rows = _source_rows("quote_slice", "td_auction_snapshot_v2")
    daily_path = (REAL.parent / "core_artifacts" / "c522" / "daily_kline" / "2026-10-23"
                  / "provisional" / "rows.jsonl.gz")
    with gzip.open(daily_path, "rt", encoding="utf-8") as stream:
        daily_rows = [json.loads(line) for line in stream]
    preclose = {row["symbol"]: round(row["pre_close"] * 1000)
                for row in daily_rows if row.get("pre_close") is not None}

    price_tested = price_passed = 0
    for row in capture_rows:
        price = row.get("px_milli")
        pct = row.get("chg_bp")
        prior = preclose.get(row.get("symbol"))
        if price is None or pct is None or prior is None:
            continue
        price_tested += 1
        price_passed += int(abs(pct - round((price / prior - 1) * 10000)) <= 1)

    limit_up_rows = [row for row in capture_rows if row.get("limit_state") == 1]
    seal_tested = [row for row in limit_up_rows if row.get("rest_bid_amt_yuan") is not None]
    seal_passed = sum(row["rest_bid_amt_yuan"] >= 0 for row in seal_tested)
    assert price_tested > 0, "TD price relation was not exercised by real rows"
    assert len(seal_tested) > 0, "TD limit-up seal amount was not exercised by real rows"
    assert price_passed == price_tested, ("td_auction_snapshot_v2", "pct_price_relation",
                                          price_passed, price_tested)
    assert seal_passed == len(seal_tested), ("td_auction_snapshot_v2", "seal_buy_nonnegative",
                                             seal_passed, len(seal_tested))
    return {
        "pct_price_relation": (price_passed, price_tested, price_passed / price_tested),
        "seal_buy_nonnegative_limit_up": (seal_passed, len(seal_tested), seal_passed / len(seal_tested)),
    }


def _source_key_matches(source_key, raw_keys):
    if "[YYYYMMDD]" not in source_key:
        return source_key in raw_keys
    pattern = re.escape(source_key).replace(r"\[YYYYMMDD\]", r"\[\d{8}\]")
    return any(re.fullmatch(pattern, raw_key) for raw_key in raw_keys)


def _check_ratios(dataset):
    spec = DATASETS[dataset]
    field_by_name = {field.name: field for field in spec.fields}
    source_names = ("baostock", "wencai", "q2_final") if dataset == "daily_kline" else (
        "redis_q2", "td_auction_snapshot_v2", "wencai_auction")
    preclose_by_symbol = _daily_preclose_by_symbol() if dataset == "quote_slice" else {}
    counts = {check.name: [0, 0] for check in spec.checks}
    for source in source_names:
        for raw_row in _source_rows(dataset, source):
            projected = _project_row(dataset, source, raw_row, field_by_name)
            if dataset == "quote_slice" and source == "wencai_auction":
                prior_close = preclose_by_symbol.get(projected.get("symbol"))
                if prior_close is not None:
                    projected["pre_close_milli"] = prior_close
            for check in spec.checks:
                required = _check_names(check.expr)
                if not required.issubset(projected) or any(projected[name] is None for name in required):
                    continue
                passed = bool(eval(check.expr, {"__builtins__": {}, "abs": abs, "round": round}, projected))
                counts[check.name][0] += 1
                counts[check.name][1] += int(passed)
    ratios = {}
    for check in spec.checks:
        tested, passed = counts[check.name]
        assert tested > 0, f"no real rows exercised {dataset}.{check.name}"
        ratio = passed / tested
        assert ratio + 1e-12 >= check.min_pass_ratio, (
            dataset, check.name, passed, tested, ratio, check.min_pass_ratio)
        ratios[check.name] = (passed, tested, ratio)
    return ratios


def test_real_fixture_keys_types_nullability_and_check_ratios_are_registered():
    empty_sources = []
    unverified_source_keys = []
    for dataset in ("daily_kline", "quote_slice"):
        spec = DATASETS[dataset]
        field_by_name = {field.name: field for field in spec.fields}
        sources = ("baostock", "wencai", "q2_final") if dataset == "daily_kline" else (
            "redis_q2", "td_auction_snapshot_v2", "wencai_auction")
        for field in spec.fields:
            assert set(field.source_units) == set(sources), (dataset, field.name, field.source_units)
            assert set(field.source_keys) == set(sources), (dataset, field.name, field.source_keys)
            assert field.semantics in {"VERIFIED", "UNVERIFIED"}
            if UNKNOWN in field.source_units.values():
                assert field.semantics == "UNVERIFIED", (dataset, field.name)
        for source in sources:
            rows = _source_key_rows(dataset, source)
            if not rows:
                empty_sources.append((dataset, source))
                continue
            raw_keys = set().union(*(row.keys() for row in rows))
            declared = []
            for field in spec.fields:
                source_key = field.source_keys[source]
                if source_key.startswith("@ctx:"):
                    assert source_key[5:] in {"symbol", "trade_date", "tag"}
                    continue
                if field.name in ARTIFACT_ONLY_SOURCE_FIELDS.get((dataset, source), set()):
                    assert source_key == field.name, (dataset, source, field.name, source_key)
                    assert field.semantics == "UNVERIFIED" and not field.serves
                    declared.append(source_key)
                    continue
                if source_key == UNKNOWN:
                    unverified_source_keys.append((dataset, source, field.name))
                    continue
                if not source_key:
                    assert field.source_units[source] == "ABSENT", (
                        dataset, source, field.name, field.source_units[source])
                if source_key:
                    assert _source_key_matches(source_key, raw_keys), (
                        dataset, source, field.name, source_key, sorted(raw_keys))
                    declared.append(source_key)
            uncovered = [raw_key for raw_key in sorted(raw_keys)
                         if raw_key not in field_by_name
                         and not any(_source_key_matches(source_key, {raw_key}) for source_key in declared)]
            assert not uncovered, f"unregistered raw keys in {dataset}/{source}: {uncovered}"
        for source in sources:
            for row in _source_rows(dataset, source):
                _project_row(dataset, source, row, field_by_name)
        assert len(spec.checks) >= 3
        assert all(0.0 <= check.min_pass_ratio <= 1.0 for check in spec.checks)
        assert _check_ratios(dataset)
    assert empty_sources == [("daily_kline", "wencai")], f"empty source fixtures must be explicit: {empty_sources}"
    assert not unverified_source_keys, f"unverified source keys: {unverified_source_keys}"
    baostock_fields = {field.name: field for field in DATASETS["daily_kline"].fields}
    assert {name: baostock_fields[name].type for name in ARTIFACT_ONLY_SOURCE_FIELDS[("daily_kline", "baostock")]} == {
        "code": str, "mismatch": bool, "turnover": float, "universe_version": str,
    }
    assert all(baostock_fields[name].canonical_unit in {"none", "UNKNOWN"}
               for name in ARTIFACT_ONLY_SOURCE_FIELDS[("daily_kline", "baostock")])
    assert _td_check_ratios()


def test_td_native_source_keys_and_measured_constraints_are_explicit():
    fields = {field.name: field for field in DATASETS["quote_slice"].fields}
    expected = {
        "price_milli": "px_milli",
        "pct_bp": "chg_bp",
        "match_amount_yuan": "match_amt_yuan",
        "seal_buy_yuan": "rest_bid_amt_yuan",
        "seal_sell_yuan": "rest_ask_amt_yuan",
        "limit_state": "limit_state",
        "source_ts": "ts",
        "trade_date": "trade_date",
        "tag": "auction_tag",
        "symbol": "symbol",
    }
    assert {name: fields[name].source_keys["td_auction_snapshot_v2"] for name in expected} == expected
    for name in ("pre_close_milli", "amount_yuan", "volume_shares"):
        assert fields[name].source_keys["td_auction_snapshot_v2"] == ""
        assert fields[name].source_units["td_auction_snapshot_v2"] == "ABSENT"

    checks = _td_check_ratios()
    assert checks["pct_price_relation"][0] == checks["pct_price_relation"][1] > 0
    assert checks["seal_buy_nonnegative_limit_up"][0] == checks["seal_buy_nonnegative_limit_up"][1] > 0


def test_c526_redis_auction_keys_are_complete_raw_captures():
    captured = _gzip_json("redis_auction_keys_20261023_0925.json.gz")
    assert set(captured) == {"a2:20261023:0925", "market:auction:20261023:0925"}
    assert all(entry["type"] == "hash" and isinstance(entry["value"], dict)
               for entry in captured.values())
