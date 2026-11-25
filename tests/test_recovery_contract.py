import pytest

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
    assert result.is_available_by(3000) is False
    assert result.remains_partial is True

    revised_result = RecoveryResultV1(
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
        resulting_revision=3,
    )
    assert revised_result.content_hash != result.content_hash
    assert revised_result.evidence_hash == result.evidence_hash


def test_recovery_cutoff_safety_compares_availability_with_the_actual_cutoff():
    plan = build_recovery_plan(
        "2026-09-18",
        "0925",
        requested_symbols=("600519",),
        missing_fields=("px_milli",),
        current_revision=1,
        requested_at_ms=1000,
        reason="missing first partial cohort",
    )

    def result(available_at_ms):
        return RecoveryResultV1(
            plan_id=plan.plan_id,
            idempotency_key=plan.idempotency_key,
            source="wencai",
            recovery_state=RECOVERY_APPLIED,
            observed_at_ms=4000,
            available_at_ms=available_at_ms,
            rows={"600519": {"px_milli": 100}},
            filled_symbols=("600519",),
            filled_fields=("px_milli",),
            base_revision=1,
            resulting_revision=2,
        )

    assert result(None).is_available_by(3000) is False
    assert result(3001).is_available_by(3000) is False
    assert result(3000).is_available_by(3000) is True
    assert result("3000").is_available_by(3000) is False
    with pytest.raises(ValueError, match="cutoff_ms"):
        result(3000).is_available_by(-1)


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


def test_recovery_merge_quarantines_conflicting_symbol_and_applies_other_symbols():
    anchor_field = "auction_anchor_0925_price_milli"
    primary = {"SH.600519": {anchor_field: 0}}
    recovery_rows = {
        "SH.600519": {anchor_field: 10_000},
        "sh.600519": {anchor_field: 10_100},
        "000001": {anchor_field: 20_000},
    }

    merged = merge_recovery_rows(primary, recovery_rows)
    reversed_merge = merge_recovery_rows(
        primary, dict(reversed(tuple(recovery_rows.items())))
    )

    assert merged.rows["SH.600519"][anchor_field] == 0
    assert merged.rows["000001"][anchor_field] == 20_000
    assert merged.filled_symbols == ("000001",)
    assert merged.invalid_symbols == ("SH.600519",)
    assert merged.source_anomaly_codes == ("DUPLICATE_NORMALIZED_SYMBOL",)
    assert reversed_merge.rows == merged.rows
    assert reversed_merge.content_hash == merged.content_hash


def test_recovery_merge_deduplicates_identical_normalized_symbol_rows():
    anchor_field = "auction_anchor_0925_price_milli"
    primary = {"SH.600519": {anchor_field: 0}}
    repeated = merge_recovery_rows(
        primary,
        {
            "SH.600519": {anchor_field: 10_000},
            "sh.600519": {anchor_field: 10_000},
        },
    )
    single = merge_recovery_rows(
        primary, {"SH.600519": {anchor_field: 10_000}}
    )

    assert repeated.rows == single.rows
    assert repeated.filled_symbols == ("SH.600519",)
    assert repeated.invalid_symbols == ()
    assert repeated.source_anomaly_codes == ()


def test_recovery_result_quarantines_conflicting_symbol_without_losing_valid_fills():
    anchor_field = "auction_anchor_0925_price_milli"

    def result(rows):
        return RecoveryResultV1(
            plan_id="plan",
            idempotency_key="key",
            source="wencai",
            recovery_state=RECOVERY_APPLIED,
            observed_at_ms=2000,
            available_at_ms=None,
            rows=rows,
            filled_symbols=("SH.600519", "000001"),
            filled_fields=(anchor_field,),
            base_revision=1,
            resulting_revision=2,
        )

    rows = {
        "SH.600519": {anchor_field: 10_000},
        "sh.600519": {anchor_field: 10_100},
        "000001": {anchor_field: 20_000},
    }
    accepted = result(rows)
    reversed_result = result(dict(reversed(tuple(rows.items()))))

    assert "SH.600519" not in accepted.rows
    assert accepted.invalid_symbols == ("SH.600519",)
    assert accepted.source_anomaly_codes == ("DUPLICATE_NORMALIZED_SYMBOL",)
    assert accepted.filled_symbols == ("000001",)
    assert accepted.rows == reversed_result.rows
    assert accepted.content_hash == reversed_result.content_hash


def test_recovery_result_quarantines_invalid_filled_symbol_and_keeps_valid_sibling():
    anchor_field = "auction_anchor_0925_price_milli"
    result = RecoveryResultV1(
        plan_id="plan",
        idempotency_key="key",
        source="wencai",
        recovery_state=RECOVERY_APPLIED,
        observed_at_ms=2000,
        available_at_ms=None,
        rows={
            "BAD": {anchor_field: 10_000},
            "000001": {anchor_field: 10_100},
        },
        filled_symbols=("BAD", "000001"),
        filled_fields=(anchor_field,),
        base_revision=1,
        resulting_revision=2,
    )

    assert result.rows == {"000001": {anchor_field: 10_100}}
    assert result.filled_symbols == ("000001",)
    assert result.filled_fields == (anchor_field,)
    assert "INVALID_SYMBOL" in result.source_anomaly_codes
    assert "INVALID_FILLED_SYMBOL_DECLARATION" in result.source_anomaly_codes


def test_recovery_result_quarantines_invalid_diagnostic_symbol_and_keeps_valid_fill():
    anchor_field = "auction_anchor_0925_price_milli"
    result = RecoveryResultV1(
        plan_id="plan",
        idempotency_key="key",
        source="wencai",
        recovery_state=RECOVERY_APPLIED,
        observed_at_ms=2000,
        available_at_ms=None,
        rows={"000001": {anchor_field: 10_100}},
        filled_symbols=("000001",),
        filled_fields=(anchor_field,),
        base_revision=1,
        resulting_revision=2,
        invalid_symbols=("BAD",),
    )

    assert result.filled_symbols == ("000001",)
    assert result.invalid_symbols == ()
    assert "INVALID_DIAGNOSTIC_SYMBOL" in result.source_anomaly_codes


@pytest.mark.parametrize(
    "anchor_field",
    (
        "auction_anchor_0920_price_milli",
        "auction_anchor_0924_price_milli",
        "auction_anchor_0925_price_milli",
    ),
)
def test_recovery_merge_treats_zero_as_missing_only_for_auction_anchor_prices(
    anchor_field,
):
    merged = merge_recovery_rows(
        {
            "600519": {
                anchor_field: 0,
                "some_counter": 0,
            }
        },
        {
            "600519": {
                anchor_field: 10100,
                "some_counter": 7,
            }
        },
    )

    assert merged.rows["600519"][anchor_field] == 10100
    assert merged.rows["600519"]["some_counter"] == 0
    assert merged.filled_symbols == ("600519",)
    assert merged.filled_fields == (anchor_field,)

    zero_only = merge_recovery_rows(
        {"600519": {anchor_field: 0}},
        {"600519": {anchor_field: 0}},
    )
    assert zero_only.rows["600519"][anchor_field] == 0
    assert zero_only.filled_symbols == ()
    assert zero_only.filled_fields == ()


def test_clean_applied_recovery_result_rejects_batch_with_no_declared_fills():
    with pytest.raises(ValueError, match="APPLIED result must identify filled symbols and fields"):
        RecoveryResultV1(
            plan_id="plan",
            idempotency_key="key",
            source="wencai",
            recovery_state=RECOVERY_APPLIED,
            observed_at_ms=2000,
            available_at_ms=None,
            rows={},
            filled_symbols=(),
            filled_fields=(),
            base_revision=1,
            resulting_revision=2,
        )
