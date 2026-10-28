"""C581 attribution checks against captured Kaipan source records."""
from __future__ import annotations

import gzip
import json
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _real_pool_sample():
    payload = json.loads((ROOT / "tests/fixtures/real/kaipan_pool_2026-10-22_L1_1444.json").read_text())
    records = [row for page in payload["info"] if isinstance(page, list)
               for row in page if isinstance(row, list) and len(row) >= 23]
    return records[0]


def _real_registry_codes():
    path = ROOT / "tests/fixtures/real/core_artifacts/c594/plate_registry/2026-10-13/current/rows.jsonl.gz"
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        return {str(json.loads(line).get("plate_code")) for line in stream}


def test_main_reason_attribution_uses_one_real_captured_reason_and_registered_code():
    from engine_core.limit_reason_theme import attribute_main_reason

    record = _real_pool_sample()
    result = attribute_main_reason(
        {"Reason": record[5], "ZSCode": [record[19]], "PZSCode": "0"},
        registry_codes=_real_registry_codes(),
    )

    assert result["main_reason"] == record[5]
    assert result["main_plate_code"] == record[19]
    assert result["reason_unknown"] is False
    assert result["plate_registry_match"] is True


def test_main_reason_attribution_retains_unregistered_real_code_as_identity_mismatch():
    from engine_core.limit_reason_theme import attribute_main_reason

    record = _real_pool_sample()
    result = attribute_main_reason(
        {"Reason": record[5], "ZSCode": ["999999"], "PZSCode": "0"},
        registry_codes=_real_registry_codes(),
    )

    assert result["main_reason"] == record[5]
    assert result["main_plate_code"] == "999999"
    assert result["plate_registry_match"] is False
    assert result["identity_mismatch_codes"] == ["999999"]


def _reason_history(symbol: str) -> dict:
    path = ROOT / f"tests/fixtures/real/kaipan_ban_reasons_2026-10-29_{symbol}_st200_0537.json"
    if not path.is_file():
        path = ROOT / f"tests/fixtures/real/kaipan_ban_reasons_2026-10-29_{symbol}_st200_0538.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _real_pool_rows(trade_date: str, suffix: str) -> list[dict]:
    from engine_core.data_pipeline.limit_pool import kaipan_row

    payload = json.loads((ROOT / f"tests/fixtures/real/kaipan_pool_{trade_date}_L1_{suffix}.json")
                         .read_text(encoding="utf-8"))
    raw_rows = [row for page in payload["info"] if isinstance(page, list)
                for row in page if isinstance(row, list) and len(row) >= 23]
    return [row for row in (kaipan_row(raw, 1, trade_date) for raw in raw_rows) if row]


def _reason_rows(payload: dict) -> list[dict]:
    symbol = str(payload["StockID"]).zfill(6)
    return [{"symbol": symbol, "event_date": item.get("Date"), "type": item.get("Type"),
             "reason": item.get("Reason"), "plate_codes": item.get("ZSCode"),
             "parent_plate_code": item.get("PZSCode")}
            for item in payload.get("List", []) if isinstance(item, dict)]


def _wencai_rows(trade_date: str) -> list[dict]:
    from engine_core.data_pipeline.limit_pool import build_wencai

    suffix = "0539"
    payload = json.loads((ROOT / f"tests/fixtures/real/wencai_limit_up_pool_{trade_date}_{suffix}.json")
                         .read_text(encoding="utf-8"))
    return build_wencai(trade_date, payload["rows"], reported_total=payload.get("reported_total")).rows


def test_sdk_adapter_rewrites_latest_only_request_to_full_history(monkeypatch):
    from urllib.parse import parse_qs, urlsplit
    from engine_core.data_pipeline.providers.kaipan_sdk_adapter import get_ban_reasons

    payload = _reason_history("002982")
    seen = []

    def request(url, *args, **kwargs):
        seen.append(url)
        return payload

    sdk = types.ModuleType("pykaipan.pykaipan")
    sdk.reques = request

    def get_ban_reasons_from_sdk(*, code):
        return sdk.reques(f"https://fixture.invalid/?a=GetDayZhangTing&StockID={code}&st=1")

    sdk.getBanReasons = get_ban_reasons_from_sdk
    package = types.ModuleType("pykaipan")
    package.__path__ = []
    monkeypatch.setitem(sys.modules, "pykaipan", package)
    monkeypatch.setitem(sys.modules, "pykaipan.pykaipan", sdk)

    result = get_ban_reasons("002982", st=200)

    assert result == payload
    assert parse_qs(urlsplit(seen[0]).query)["st"] == ["200"]
    assert sdk.reques is request


def test_provider_requests_full_history_checkpoints_and_excludes_future_reason(tmp_path):
    from engine_core.data_pipeline.providers.kaipan_provider import KaipanProvider

    payload = _reason_history("003021")

    class Client:
        calls = []

        def get_ban_reasons(self, symbol, *, st):
            self.calls.append((symbol, st))
            return payload

    client = Client()
    checkpoint = tmp_path / "kaipan-limit-reasons-checkpoint.json"
    provider = KaipanProvider(client, symbols=["003021"], max_requests=1, min_interval_seconds=0.2,
                              sleep_fn=lambda _seconds: None, reasons_checkpoint_path=checkpoint)
    result = provider.limit_reasons("2026-10-22", symbols=["003021"])

    assert client.calls == [("003021", 200)]
    assert result["history_page_size"] == 200
    dates = {row["event_date"] for row in result["rows"]}
    assert "2026-10-22" in dates
    assert all(event_date <= "2026-10-22" for event_date in dates)
    assert json.loads(checkpoint.read_text(encoding="utf-8"))["done_symbols"] == ["003021"]


def test_real_sources_follow_kaipan_pool_then_history_then_wencai_priority():
    from engine_core.data_pipeline.limit_reasons import build_limit_reasons

    # 2026-10-23 has a captured Kaipan pool row and detailed API history for 002982.
    pool_23 = _real_pool_rows("2026-10-23", "0537")
    row_2982 = next(row for row in pool_23 if row["symbol"] == "002982")
    history_2982 = _reason_rows(_reason_history("002982"))
    wc_23 = _wencai_rows("2026-10-23")
    registry = [{"plate_code": code} for code in _real_registry_codes()]

    direct = build_limit_reasons("2026-10-23", [row_2982], kaipan_rows=history_2982,
                                 wencai_rows=wc_23, plate_registry_rows=registry,
                                 plate_registry_loaded=True, pool_source="kaipan_getHisBans")
    assert direct["rows"][0]["source"] == "KAIPAN_GET_HIS_BANS"
    assert direct["rows"][0]["main_reason"] == "三季报增长"
    assert direct["rows"][0]["plate_code"] == "801573"
    assert direct["rows"][0]["plate_registry_match"] is True

    sparse_pool = {**row_2982, "reason_category": None}
    api_first = build_limit_reasons("2026-10-23", [sparse_pool], kaipan_rows=history_2982,
                                   wencai_rows=wc_23, plate_registry_rows=registry,
                                   plate_registry_loaded=True, pool_source="kaipan_getHisBans")
    assert api_first["rows"][0]["source"] == "KAIPAN_GET_BAN_REASONS"
    assert api_first["rows"][0]["main_reason"] == "三季报增长"

    pool_26 = _real_pool_rows("2026-10-26", "0540")
    row_411 = next(row for row in pool_26 if row["symbol"] == "000411")
    row_411["reason_category"] = None
    wencai_26 = _wencai_rows("2026-10-26")
    wencai_first = build_limit_reasons("2026-10-26", [row_411], wencai_rows=wencai_26,
                                       plate_registry_rows=registry, plate_registry_loaded=True,
                                       pool_source="kaipan_getHisBans")
    assert wencai_first["rows"][0]["source"] == "WENCAI_LIMIT_UP_POOL"
    assert wencai_first["rows"][0]["main_reason"] == "三季报单季增长"


def test_real_historical_fallback_is_degraded_and_target_day_is_not_looked_ahead():
    from engine_core.data_pipeline.limit_reasons import build_limit_reasons

    pool = _real_pool_rows("2026-10-23", "0537")
    row = next(item for item in pool if item["symbol"] == "002982")
    row["reason_category"] = None
    # Replaying only the actually captured earlier Type 0 entries models a provider history that has no target row.
    older = [item for item in _reason_rows(_reason_history("002982"))
             if item["type"] == "0" and item["event_date"] < "2026-10-23"]
    fallback = build_limit_reasons("2026-10-23", [row], kaipan_rows=older,
                                   plate_registry_rows=[{"plate_code": code} for code in _real_registry_codes()],
                                   plate_registry_loaded=True, pool_source="kaipan_getHisBans")
    assert fallback["rows"][0]["source"] == "HISTORICAL_ATTRIBUTION"
    assert fallback["rows"][0]["status"] == "DEGRADED"
    assert fallback["rows"][0]["event_date"] == "2026-07-01"

    pool_22_payload = json.loads((ROOT / "tests/fixtures/real/kaipan_pool_2026-10-22_L1_1444.json")
                                 .read_text(encoding="utf-8"))
    from engine_core.data_pipeline.limit_pool import kaipan_row
    raw_003021 = next(raw for page in pool_22_payload["info"] if isinstance(page, list)
                      for raw in page if isinstance(raw, list) and str(raw[0]).zfill(6) == "003021")
    pool_003021 = kaipan_row(raw_003021, 1, "2026-10-22")
    pool_003021["reason_category"] = None
    lookahead = build_limit_reasons("2026-10-22", [pool_003021],
                                    kaipan_rows=_reason_rows(_reason_history("003021")),
                                    pool_source="kaipan_getHisBans")
    assert lookahead["rows"][0]["event_date"] == "2026-10-22"
    assert lookahead["rows"][0]["event_date"] != "2026-10-23"

    future_only = [row for row in _reason_rows(_reason_history("003021"))
                   if row["event_date"] == "2026-10-23"]
    no_lookahead = build_limit_reasons("2026-10-22", [pool_003021], kaipan_rows=future_only,
                                       pool_source="kaipan_getHisBans")
    assert no_lookahead["status"] == "MISSING"
    assert no_lookahead["rows"] == []


def test_all_sources_failed_is_typed_missing_without_fabricated_rows():
    from engine_core.data_pipeline.limit_reasons import build_limit_reasons

    pool_row = _real_pool_rows("2026-10-23", "0537")[0]
    pool_row["reason_category"] = None
    result = build_limit_reasons("2026-10-23", [pool_row], pool_source="kaipan_getHisBans")

    assert result["status"] == "MISSING"
    assert result["error_code"] == "ALL_SOURCES_FAILED"
    assert result["expected_count"] == 1
    assert result["coverage_count"] == 0
    assert result["rows"] == []
