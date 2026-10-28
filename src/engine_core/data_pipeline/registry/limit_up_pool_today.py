from .spec import DatasetSpec, FieldSpec


def _sources(redis_q2: str, wencai: str) -> dict[str, str]:
    return {"redis_q2": redis_q2, "wencai": wencai,
            "redis_q2+wencai": "redis_membership; wencai_enrichment"}


SPEC = DatasetSpec(
    "limit_up_pool_today", "redis_q2>wencai", "kaipan_hot_* aligned; 15:20 close confirmation",
    primary_backend="redis", hot_view=True, redis_ttl_seconds=604800,
    max_redis_payload_bytes=1048576, dependencies=("symbol_universe",), point_in_time=True,
    source_dependency="legacy_readonly", fetch_budget_scope="redis_q2",
    empty_is_valid=True, key_fields=("symbol", "date", "as_of"),
    required_fields=("symbol", "date", "as_of", "pct", "limit_up", "seal_amount", "source"),
    primary_providers=("redis_q2",), source_preference=("redis_q2", "wencai"),
    fields=(
        FieldSpec("symbol", str, False, "none", _sources("none:six_digit_code", "none:six_digit_code"),
                  semantics="VERIFIED", description="Six digit security identifier."),
        FieldSpec("date", str, False, "iso_date", _sources("key_date:YYYYMMDD", "query_date:YYYY-MM-DD"),
                  semantics="VERIFIED", description="Described trade date."),
        FieldSpec("as_of", str, False, "iso_datetime", _sources("sample_time:Asia/Shanghai", "sample_time:Asia/Shanghai"),
                  semantics="VERIFIED", description="Actual sampling time; every source timestamp must be <= this value."),
        FieldSpec("pct", float, True, "percent", _sources("derived:px/pc_percent", "dated_column:percent"),
                  semantics="VERIFIED", description="Price change in percent, not basis points."),
        FieldSpec("limit_up", bool, False, "none", _sources("derived:board_price_limit", "query_match:boolean"),
                  semantics="VERIFIED", description="True when the current price equals the board-rounded limit price."),
        FieldSpec("limit_price", float, True, "yuan", _sources("derived:board_price_limit", "ABSENT"),
                  semantics="VERIFIED", description="Limit-up price rounded half-up to one cent."),
        FieldSpec("seal_amount", float, True, "yuan", _sources("ABSENT", "dated_column:元|万元|亿元"),
                  semantics="UNVERIFIED", description="Null unless a dated source amount and its unit are verifiable."),
        FieldSpec("first_limit_time", str, True, "local_time", _sources("ABSENT", "dated_column:HH:MM:SS"),
                  semantics="UNVERIFIED", description="First limit-up time from WenCai when date evidence matches."),
        FieldSpec("final_limit_time", str, True, "local_time", _sources("ABSENT", "dated_column:HH:MM:SS"),
                  semantics="UNVERIFIED", description="Final limit-up time from WenCai when date evidence matches."),
        FieldSpec("open_count", int, True, "count", _sources("ABSENT", "dated_column:count"),
                  semantics="UNVERIFIED", description="Intraday opening count from WenCai when date evidence matches."),
        FieldSpec("source_ts", str, True, "iso_datetime", _sources("q2.ts:unix_milliseconds", "ABSENT"),
                  semantics="VERIFIED", description="Q2 quote timestamp; rows later than as_of are removed."),
        FieldSpec("st_status", str, False, "none", _sources("symbol_universe.name", "query:remove_st"),
                  semantics="VERIFIED", description="NON_ST only with an available name or WenCai's remove-ST query; otherwise UNAVAILABLE."),
        FieldSpec("redis_phase", str, True, "none", _sources("q2.ph", "ABSENT"),
                  semantics="VERIFIED", description="Q2 market phase, retained for terminal close checks."),
        FieldSpec("wencai_confirmed", bool, True, "none", _sources("ABSENT", "query_member"),
                  semantics="VERIFIED", description="Whether the bounded WenCai close query returned the symbol."),
        FieldSpec("source", str, False, "none", _sources("redis_q2", "wencai"),
                  semantics="VERIFIED", description="Per-row provider attribution."),
    ),
)
