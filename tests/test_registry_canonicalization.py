from __future__ import annotations

import gzip
import json
from dataclasses import replace
from pathlib import Path

import pytest

from engine_core.data_pipeline.registry import DATASETS
from engine_core.data_pipeline.registry.canonical import canonicalize_row
from engine_core.data_pipeline.registry.checks import evaluate_checks
from engine_core.data_pipeline.registry.convert import ConversionError, to_canonical
from engine_core.data_pipeline.registry.gate import summarize_gate
from engine_core.data_pipeline.registry.resolve import resolve_adapter
from engine_core.data_pipeline.registry.spec import CheckSpec, _validate_check_expression
from engine_core.data_pipeline.registry.units import parse_source_code


FIXTURES = Path(__file__).parent / "fixtures" / "real"


def _gzip_json(path: Path):
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return json.load(stream)


def _core_daily_rows():
    path = FIXTURES / "core_artifacts/c522/daily_kline/2026-10-23/formal/rows.jsonl.gz"
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def test_decimal_conversion_uses_half_up_and_preserves_missing_values():
    assert to_canonical(9.69, "yuan:float", "price_milli") == 9690
    assert to_canonical(-0.9356, "percent:float", "bp") == -94
    assert to_canonical(726344395.85, "yuan:float", "yuan") == 726344396
    assert to_canonical("--", "yuan:numeric_string", "yuan") is None
    assert to_canonical("", "yuan:numeric_string", "yuan") is None
    assert to_canonical(None, "yuan:float", "yuan") is None
    assert to_canonical("2.5", "milli_yuan:numeric_string", "price_milli") == ConversionError.NON_INTEGER
    assert to_canonical("not-a-number", "yuan:numeric_string", "yuan") == ConversionError.UNPARSEABLE


def test_registered_units_and_check_expressions_are_closed_and_parseable():
    for dataset_name in ("daily_kline", "quote_slice"):
        spec = DATASETS[dataset_name]
        field_names = {field.name for field in spec.fields}
        for field in spec.fields:
            for source_code in field.source_units.values():
                parsed = parse_source_code(source_code)
                assert parsed.special in {"UNKNOWN", "ABSENT"} or (
                    parsed.unit is not None and parsed.encoding is not None
                )
        for check in spec.checks:
            _validate_check_expression(check.expr, field_names)
    with pytest.raises(ValueError):
        _validate_check_expression("__import__('os').system('true')")


def test_resolver_uses_real_manifest_provider_attempt_and_rejects_composite_label():
    manifest_path = FIXTURES / "c528" / "daily_kline_baostock_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    spec = DATASETS["daily_kline"]
    assert resolve_adapter(spec, manifest, "baostock>wencai>q2_final").adapter == "baostock"
    assert resolve_adapter(spec, {}, "baostock>wencai>q2_final").adapter is None
    assert resolve_adapter(spec, {}, "baostock").adapter == "baostock"


def test_adapter_resolution_uses_real_row_shapes_alias_and_fails_ambiguous_shapes():
    spec = DATASETS["daily_kline"]
    baostock_rows = _core_daily_rows()
    q2_rows = _gzip_json(FIXTURES / "c526" / "daily_kline_q2_final_first200.json.gz")["rows"]

    inferred = resolve_adapter(
        spec, {}, "baostock>wencai>q2_final", sample_rows=baostock_rows,
    )
    assert (inferred.adapter, inferred.resolved_by) == ("baostock", "row_shape")

    aliased = resolve_adapter(
        spec, {}, "legacy_q2_final", sample_rows=q2_rows,
    )
    assert (aliased.adapter, aliased.resolved_by) == ("q2_final", "alias")

    # Both samples are captured real rows; their shared required keys and distinct
    # native markers intentionally make the shape evidence ambiguous.
    ambiguous = resolve_adapter(
        spec, {}, "baostock>wencai>q2_final",
        sample_rows=baostock_rows[:1] + q2_rows[:199],
    )
    assert (ambiguous.adapter, ambiguous.resolved_by) == (None, "unresolved")

    wencai_rows = json.loads(
        (FIXTURES / "c526" / "daily_kline_wencai_first200.json").read_text(encoding="utf-8")
    )["rows"]
    assert resolve_adapter(spec, {}, "unknown", sample_rows=wencai_rows).adapter is None


def test_forcing_baostock_on_real_q2_rows_fails_volume_price_check():
    spec = DATASETS["daily_kline"]
    q2_rows = _gzip_json(FIXTURES / "c526" / "daily_kline_q2_final_first200.json.gz")["rows"]
    summary = summarize_gate(
        spec, q2_rows[:1], trade_date="2026-10-23", manifest={},
        source="baostock>wencai>q2_final", adapter="baostock",
    )
    assert summary["adapter_resolved_by"] == "adapter"
    assert summary["checks"]["turnover_average_price_in_range"]["status"] in {
        "BELOW_MIN", "FAIL",
    }


def test_unresolved_adapter_leaves_key_validity_unknown_for_real_ambiguous_rows():
    spec = DATASETS["daily_kline"]
    rows = _core_daily_rows()[:1] + _gzip_json(
        FIXTURES / "c526" / "daily_kline_q2_final_first200.json.gz"
    )["rows"][:199]
    summary = summarize_gate(
        spec, rows, trade_date="2026-10-23", manifest={},
        source="baostock>wencai>q2_final",
    )
    assert summary["adapter"] is None
    assert summary["key_invalid"] is None
    assert summary["flagged_rows"] is None
    assert summary["reasons"] == ["adapter_unresolved"]
    assert summary["gate_status"] == "FAIL"


def test_real_rows_normalize_and_check_evaluation_preserves_row_count():
    spec = DATASETS["daily_kline"]
    capture = _gzip_json(FIXTURES / "c526" / "daily_kline_q2_final_first200.json.gz")
    raw_rows = capture["rows"]
    canonical_rows = [canonicalize_row(spec, "q2_final", row) for row in raw_rows]
    assert len(canonical_rows) == len(raw_rows) == 200
    assert all(not row.unmapped_keys for row in canonical_rows)
    stats = evaluate_checks(spec, canonical_rows)
    assert set(stats) == {check.name for check in spec.checks}
    assert all(item["evaluated"] + item["skipped"] == len(raw_rows) for item in stats.values())
    turnover_check = next(check for check in spec.checks if check.name == "turnover_average_price_in_range")
    assert "0.5 * 1000 / volume + 1" in turnover_check.expr
    assert turnover_check.min_pass_ratio == 1.0
    assert stats[turnover_check.name]["status"] == "OK"
    assert all(next(field for field in spec.fields if field.name == name).nullable
               for name in ("amount_yuan", "pct_chg", "volume"))

    missing_symbol = dict(raw_rows[0])
    missing_symbol.pop("symbol")
    missing_result = canonicalize_row(spec, "q2_final", missing_symbol)
    assert missing_result.values["symbol"] is None
    assert missing_result.flags["symbol"] == "MISSING_REQUIRED"
    missing_date = dict(raw_rows[0])
    missing_date.pop("date")
    assert canonicalize_row(spec, "q2_final", missing_date).flags["date"] == "NULL_NOT_ALLOWED"

    unknown_result = canonicalize_row(spec, None, raw_rows[0])
    assert set(unknown_result.flags.values()) == {"ADAPTER_UNKNOWN"}

    quote_spec = DATASETS["quote_slice"]
    redis_hashes = _gzip_json(FIXTURES / "c526" / "redis_q2_hashes_20261023_first60.json.gz")
    symbol, item = next(iter(redis_hashes.items()))
    redis_raw = item["value"]
    redis_capture_row = {
        "symbol": symbol, "trade_date": "2026-10-23", "tag": "0925", **redis_raw,
    }
    redis_row = canonicalize_row(quote_spec, "redis_q2", redis_capture_row)
    assert redis_row.values["a25"] == int(redis_raw["a25"])
    assert redis_row.values["volume_shares"] == int(float(redis_raw["vol"]) * 100)
    assert redis_row.values["symbol"] == symbol
    assert redis_row.values["trade_date"] == "2026-10-23"
    assert redis_row.values["tag"] == "0925"
    assert redis_row.values["source_ts"].endswith("+08:00")
    assert not redis_row.unmapped_keys
    for name in ("amt2m", "amt5m", "ia", "iv", "ln", "mn", "mx", "spd1m", "vec3m", "vec5m"):
        assert redis_row.values[name] == redis_raw[name]
        assert name not in redis_row.flags
    missing_context = canonicalize_row(quote_spec, "redis_q2", redis_raw)
    assert {name: missing_context.flags[name] for name in ("symbol", "trade_date", "tag")} == {
        "symbol": "MISSING_REQUIRED", "trade_date": "MISSING_REQUIRED", "tag": "MISSING_REQUIRED"
    }

    td_capture = _gzip_json(FIXTURES / "c527" / "td_auction_snapshot_v2_20261023_first70.json.gz")
    td_raw = dict(zip(td_capture["cols"], td_capture["rows"][0]))
    td_row = canonicalize_row(quote_spec, "td_auction_snapshot_v2", td_raw)
    assert td_row.values["trade_date"] == "2026-10-23"
    assert td_row.values["price_milli"] == td_raw["px_milli"]
    assert td_row.values["source_ts"].endswith("+08:00")

    wencai_capture = _gzip_json(FIXTURES / "c527" / "wencai_auction_upstream_first60.json.gz")
    wencai_row = canonicalize_row(
        quote_spec, "wencai_auction", wencai_capture["rows"][0],
        context={"trade_date": "2026-10-23", "tag": "0925"},
    )
    assert wencai_row.values["price_milli"] is not None
    assert wencai_row.values["trade_date"] == "2026-10-23"
    assert wencai_row.values["tag"] == "0925"
    assert not wencai_row.unmapped_keys


def test_check_status_reports_inert_inapplicable_and_below_min_from_real_rows():
    quote_spec = DATASETS["quote_slice"]
    redis_hashes = _gzip_json(FIXTURES / "c526" / "redis_q2_hashes_20261023_first60.json.gz")
    symbol, item = next(iter(redis_hashes.items()))
    redis_row = canonicalize_row(
        quote_spec, "redis_q2", item["value"],
        context={"symbol": symbol, "trade_date": "2026-10-23", "tag": "0925"},
    )
    inert = evaluate_checks(quote_spec, [redis_row])["pct_price_relation"]
    assert inert["status"] == "INERT"
    assert inert["evaluated"] == 0 and inert["skipped"] == 1
    redis_stats = evaluate_checks(quote_spec, [redis_row], adapter="redis_q2")
    assert redis_stats["pct_price_relation"]["status"] == "INAPPLICABLE"
    assert redis_stats["match_amount_le_turnover"]["status"] == "OK"

    td_capture = _gzip_json(FIXTURES / "c527" / "td_auction_snapshot_v2_20261023_first70.json.gz")
    td_rows = [dict(zip(td_capture["cols"], row)) for row in td_capture["rows"]]
    td_canonical = [canonicalize_row(quote_spec, "td_auction_snapshot_v2", row) for row in td_rows]
    td_stats = evaluate_checks(quote_spec, td_canonical, adapter="td_auction_snapshot_v2")
    assert td_stats["pct_price_relation"]["status"] == "INAPPLICABLE"
    assert td_stats["match_amount_le_turnover"]["status"] == "INAPPLICABLE"
    assert td_stats["price_positive_when_present"]["status"] == "OK"
    assert td_stats["seal_sell_nonnegative"]["status"] == "OK"

    failing_check = CheckSpec("real_rows_fail_probe", "limit_state in (-2, 7)", 1.0)
    failing_spec = replace(quote_spec, checks=(failing_check,))
    below_min = evaluate_checks(failing_spec, td_canonical, adapter="td_auction_snapshot_v2")[
        "real_rows_fail_probe"
    ]
    assert below_min["status"] == "BELOW_MIN"
    assert below_min["evaluated"] > 0 and below_min["passed"] < below_min["evaluated"]
