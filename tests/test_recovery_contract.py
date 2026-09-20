from engine_core import (
    RECOVERY_APPLIED,
    RecoveryResultV1,
    build_recovery_plan,
    merge_recovery_rows,
)


def test_recovery_plan_is_idempotent_and_result_keeps_unknown_available_at():
    plan = build_recovery_plan(
        "2026-09-18",
        "0925",
        requested_symbols=("600519",),
        missing_fields=("px_milli",),
        current_revision=1,
        requested_at_ms=1000,
        reason="missing first partial cohort",
    )
    repeat = build_recovery_plan(
        "2026-09-18",
        "0925",
        requested_symbols=("600519",),
        missing_fields=("px_milli",),
        current_revision=1,
        requested_at_ms=2000,
        reason="same request",
    )
    assert plan.idempotency_key == repeat.idempotency_key
    result = RecoveryResultV1(
        plan_id=plan.plan_id,
        idempotency_key=plan.idempotency_key,
        source="wencai",
        recovery_state=RECOVERY_APPLIED,
        observed_at_ms=2000,
        available_at_ms=None,
        rows={"600519": {"px_milli": 100}},
        filled_symbols=("600519",),
        filled_fields=("px_milli",),
        base_revision=1,
        resulting_revision=2,
    )
    assert result.historical_cutoff_safe is False
    assert result.remains_partial is True


def test_recovery_only_fills_missing_values_and_never_overwrites_primary():
    merged = merge_recovery_rows(
        {"600519": {"px_milli": 101, "pc_milli": None}},
        {"600519": {"px_milli": 999, "pc_milli": 100}, "000001": {"px_milli": 20}},
    )
    assert merged.rows["600519"]["px_milli"] == 101
    assert merged.rows["600519"]["pc_milli"] == 100
    assert merged.rows["000001"]["px_milli"] == 20
    assert merged.recovery_state == RECOVERY_APPLIED
    assert merged.filled_symbols == ("000001", "600519")
