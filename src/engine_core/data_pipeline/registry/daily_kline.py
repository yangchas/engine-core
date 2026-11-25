from collections.abc import Mapping
import math

from .spec import CheckSpec, DatasetSpec, FieldSpec


DAILY_KLINE_QUALITY_FIELDS = (
    "open", "high", "low", "close", "pre_close", "pct_chg", "amount_yuan", "volume",
)

BAOSTOCK_COMPLETION_POLICY = {
    "formal_coverage_threshold": 0.995,
    "source_empty_max": 20,
}


def classify_daily_kline(provider: str, coverage: float | None,
                         field_completeness: Mapping[str, str] | object) -> str:
    """Classify business readiness from the actual provider and measured quality."""
    if (isinstance(coverage, bool) or not isinstance(coverage, (int, float))
            or not math.isfinite(coverage)):
        return "PROVISIONAL"
    if coverage < 0.98:
        return "PARTIAL"
    if provider != "baostock" or not isinstance(field_completeness, Mapping):
        return "PROVISIONAL"
    if all(str(field_completeness.get(field, "")).upper() in {"COMPLETE", "SUSPENDED_ONLY"}
           for field in DAILY_KLINE_QUALITY_FIELDS):
        return "FORMAL"
    return "PROVISIONAL"


def _source_keys(baostock: str, wencai: str, q2_final: str) -> dict[str, str]:
    return {"baostock": baostock, "wencai": wencai, "q2_final": q2_final}


SPEC = DatasetSpec("daily_kline", "baostock>wencai>q2_final", "15:05_degrade_17:30_formal",
                redis_ttl_seconds=7 * 86400, max_redis_payload_bytes=65536,
                dependencies=("trade_calendar", "symbol_universe"),
                source_dependency="tiered_baostock_wencai_legacy_redis_q2", key_fields=("symbol",),
                required_fields=("symbol",), source_aliases={"legacy_q2_final": "q2_final"},
                min_coverage=0.95, fields=(
    FieldSpec("symbol", str, False, "none", {"baostock": "none:str", "wencai": "UNKNOWN", "q2_final": "none:str"},
              ("D-T1-22", "D-T1-23"), "UNVERIFIED", "Six digit stock identifier.",
              source_keys=_source_keys("symbol", "UNKNOWN", "symbol")),
    FieldSpec("date", str, False, "iso_date", {"baostock": "iso_date:str", "wencai": "UNKNOWN", "q2_final": "iso_date:str"},
              ("D-T5-08",), "UNVERIFIED", "Trade date in ISO YYYY-MM-DD form.",
              source_keys=_source_keys("date", "UNKNOWN", "date")),
    FieldSpec("open", int, True, "price_milli", {"baostock": "yuan:float", "wencai": "UNKNOWN", "q2_final": "yuan:float"},
              ("D-T1-12",), "UNVERIFIED", "Opening price, represented in canonical milliyuan.",
              source_keys=_source_keys("open", "UNKNOWN", "open")),
    FieldSpec("high", int, False, "price_milli", {"baostock": "yuan:float", "wencai": "UNKNOWN", "q2_final": "yuan:float"},
              ("D-T1-12",), "UNVERIFIED", "Session high, represented in canonical milliyuan.",
              source_keys=_source_keys("high", "UNKNOWN", "high")),
    FieldSpec("low", int, False, "price_milli", {"baostock": "yuan:float", "wencai": "UNKNOWN", "q2_final": "yuan:float"},
              ("D-T1-12",), "UNVERIFIED", "Session low, represented in canonical milliyuan.",
              source_keys=_source_keys("low", "UNKNOWN", "low")),
    FieldSpec("close", int, False, "price_milli", {"baostock": "yuan:float", "wencai": "UNKNOWN", "q2_final": "yuan:float"},
              ("D-T1-12", "D-T1-22"), "UNVERIFIED", "Closing price, represented in canonical milliyuan.",
              source_keys=_source_keys("close", "UNKNOWN", "close")),
    FieldSpec("pre_close", int, False, "price_milli", {"baostock": "yuan:float", "wencai": "UNKNOWN", "q2_final": "yuan:float"},
              ("D-T1-12",), "UNVERIFIED", "Previous close, represented in canonical milliyuan.",
              source_keys=_source_keys("pre_close", "UNKNOWN", "pre_close")),
    FieldSpec("pct_chg", int, True, "bp", {"baostock": "percent:float", "wencai": "UNKNOWN", "q2_final": "percent:float"},
              ("D-T1-22",), "UNVERIFIED", "Daily percentage change in basis points; null for suspended/no-trade rows.",
              source_keys=_source_keys("pct_chg", "UNKNOWN", "pct_chg")),
    FieldSpec("amount_yuan", float, True, "yuan", {"baostock": "yuan:float", "wencai": "UNKNOWN", "q2_final": "yuan:float"},
              ("D-T1-22",), "UNVERIFIED", "Session turnover amount in yuan; null for suspended/no-trade rows.",
              source_keys=_source_keys("amount_yuan", "UNKNOWN", "amount_yuan")),
    FieldSpec("volume", int, True, "shares", {"baostock": "shares:float", "wencai": "UNKNOWN", "q2_final": "hands:float"},
              ("D-T1-22",), "UNVERIFIED", "Session volume normalized to shares; null for suspended/no-trade rows; q2_final hand values are multiplied by 100.",
              source_keys=_source_keys("volume", "UNKNOWN", "volume")),
    FieldSpec("turnover_pct", float, True, "percent", {"baostock": "percent:float", "wencai": "UNKNOWN", "q2_final": "ABSENT"},
              (), "UNVERIFIED", "Turnover ratio in percent; only present in the Baostock capture.",
              source_keys=_source_keys("turnover_pct", "UNKNOWN", "")),
    FieldSpec("suspended", bool, True, "none", {"baostock": "none:bool", "wencai": "UNKNOWN", "q2_final": "ABSENT"},
              (), "UNVERIFIED", "Provider suspension flag.",
              source_keys=_source_keys("suspended", "UNKNOWN", "")),
    FieldSpec("open_missing", bool, True, "none", {"baostock": "ABSENT", "wencai": "UNKNOWN", "q2_final": "none:bool"},
              (), "UNVERIFIED", "q2_final flag that the opening price is absent.",
              source_keys=_source_keys("", "UNKNOWN", "open_missing")),
    FieldSpec("quality", str, True, "none", {"baostock": "ABSENT", "wencai": "UNKNOWN", "q2_final": "category:str"},
              (), "UNVERIFIED", "q2_final row quality label.",
              source_keys=_source_keys("", "UNKNOWN", "quality")),
    FieldSpec("source", str, True, "none", {"baostock": "UNKNOWN", "wencai": "UNKNOWN", "q2_final": "category:str"},
              (), "UNVERIFIED", "Source label retained without interpreting its historical meaning.",
              source_keys=_source_keys("source", "UNKNOWN", "source"), raw_only=True),
    FieldSpec("available_at", str, True, "none", {"baostock": "UNKNOWN", "wencai": "UNKNOWN", "q2_final": "ABSENT"},
              (), "UNVERIFIED", "Historical row availability value retained as supplied.",
              source_keys=_source_keys("available_at", "UNKNOWN", ""), raw_only=True),
    FieldSpec("code", str, True, "none", {"baostock": "none:str", "wencai": "UNKNOWN", "q2_final": "ABSENT"},
              (), "UNVERIFIED", "Baostock market-prefixed code retained as supplied.",
              source_keys=_source_keys("code", "UNKNOWN", "")),
    FieldSpec("mismatch", bool, True, "none", {"baostock": "none:bool", "wencai": "UNKNOWN", "q2_final": "ABSENT"},
              (), "UNVERIFIED", "Production artifact mismatch marker; comparison semantics are not established here.",
              source_keys=_source_keys("mismatch", "UNKNOWN", "")),
    FieldSpec("turnover", float, True, "UNKNOWN", {"baostock": "UNKNOWN", "wencai": "UNKNOWN", "q2_final": "ABSENT"},
              (), "UNVERIFIED", "Production artifact turnover field; canonical unit remains unknown.",
              source_keys=_source_keys("turnover", "UNKNOWN", ""), raw_only=True),
    FieldSpec("universe_version", str, True, "none", {"baostock": "none:str", "wencai": "UNKNOWN", "q2_final": "ABSENT"},
              (), "UNVERIFIED", "Universe version identifier carried by production artifacts.",
              source_keys=_source_keys("universe_version", "UNKNOWN", "")),
), checks=(
    CheckSpec("ohlc_range", "low <= close <= high", 1.0),
    CheckSpec("pct_close_preclose", "abs(pct_chg - round((close / pre_close - 1) * 10000)) <= 1", 1.0),
    CheckSpec("turnover_average_price_in_range",
              "volume == 0 or low - (0.5 * 1000 / volume + 1) <= amount_yuan / volume * 1000 <= high + (0.5 * 1000 / volume + 1)", 1.0),
))
