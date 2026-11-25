import pytest

from examples.audit_task008_anchor_q2_plate_impact import (
    summarize_plate_candidate_impact,
)


def _field(td_value, q2_value, *, td_status="PRESENT", q2_status="PRESENT"):
    if td_status == "PRESENT" and q2_status == "PRESENT":
        comparison = "EQUAL" if td_value == q2_value else "DIFFERENT"
    elif q2_status == "NULL":
        comparison = "Q2_NULL"
    elif q2_status == "NO_CANDIDATE":
        comparison = "NO_Q2_CANDIDATE"
    else:
        comparison = "TD_NULL"
    return {
        "td_value": td_value,
        "td_status": td_status,
        "q2_value": q2_value,
        "q2_status": q2_status,
        "comparison": comparison,
    }


def _row(tag, symbol, *, am, br, ar):
    return {
        "auction_tag": tag,
        "symbol": symbol,
        "fields": {"am": am, "br": br, "ar": ar},
    }


def test_plate_sums_keep_zero_and_exclude_unmapped_cohort_members():
    summary, per_plate = summarize_plate_candidate_impact(
        [
            _row(
                "0920",
                "600000",
                am=_field(0, 0),
                br=_field(100, 90),
                ar=_field(5, 5),
            ),
            _row(
                "0920",
                "600001",
                am=_field(200, 180),
                br=_field(0, 5),
                ar=_field(20, 18),
            ),
            _row(
                "0920",
                "999999",
                am=_field(50, 60),
                br=_field(10, 10),
                ar=_field(20, 20),
            ),
        ],
        {"trade_date": "20260930", "effective_time": "08:30:00", "mapping": {
            "600000": "plate-A",
            "600001": "plate-A",
            "600002": "plate-B",
        }},
        trade_date="2026-09-30",
    )

    tag = summary["tags"]["0920"]
    assert tag["observed_td_rows"] == 3
    assert tag["mapped_td_rows"] == 2
    assert tag["unmapped_td_rows"] == 1
    assert tag["unmapped_symbols"] == ["999999"]
    assert tag["mapping_members_not_observed_count"] == 1
    assert tag["fields"]["am"]["td_value_sum"] == 200
    assert tag["fields"]["am"]["q2_value_sum"] == 180
    assert tag["fields"]["am"]["signed_difference_sum"] == -20
    assert tag["fields"]["am"]["gross_absolute_difference_sum"] == 20
    assert tag["plate_partition_reconciliation"] == {
        "am": "PASS",
        "br": "PASS",
        "ar": "PASS",
    }
    plate_a = next(row for row in per_plate if row["plate"] == "plate-A" and row["auction_tag"] == "0920")
    assert plate_a["mapping_member_count"] == 2
    assert plate_a["observed_member_count"] == 2
    assert plate_a["fields"]["am"]["compared_count"] == 2
    assert plate_a["fields"]["br"]["gross_absolute_difference_sum"] == 15


def test_null_candidate_is_not_coerced_to_zero_in_plate_totals():
    summary, per_plate = summarize_plate_candidate_impact(
        [
            _row(
                "0924",
                "600000",
                am=_field(0, 0),
                br=_field(100, None, q2_status="NULL"),
                ar=_field(0, 0),
            )
        ],
        {"trade_date": "20260930", "mapping": {"600000": "plate-A"}},
        trade_date="20260930",
    )

    report = summary["tags"]["0924"]["fields"]["br"]
    assert report["comparison_status_counts"]["Q2_NULL"] == 1
    assert report["compared_count"] == 0
    assert report["td_value_sum"] is None
    assert report["q2_value_sum"] is None
    assert report["signed_difference_sum"] is None
    plate_a = next(row for row in per_plate if row["plate"] == "plate-A" and row["auction_tag"] == "0924")
    assert plate_a["fields"]["br"]["comparison_status_counts"]["Q2_NULL"] == 1


def test_mapping_trade_date_mismatch_is_rejected():
    with pytest.raises(ValueError, match="mapping trade_date"):
        summarize_plate_candidate_impact(
            [],
            {"trade_date": "20260929", "mapping": {}},
            trade_date="20260930",
        )


def test_duplicate_anchor_symbol_is_rejected_not_double_counted():
    row = _row(
        "0925",
        "600000",
        am=_field(1, 1),
        br=_field(2, 2),
        ar=_field(3, 3),
    )
    with pytest.raises(ValueError, match="duplicate anchor symbol"):
        summarize_plate_candidate_impact(
            [row, row],
            {"trade_date": "20260930", "mapping": {"600000": "plate-A"}},
            trade_date="20260930",
        )


def test_unknown_field_status_is_rejected_instead_of_counted_as_missing():
    row = _row(
        "0925",
        "600000",
        am=_field(None, None, td_status="NOT_A_STATUS", q2_status="NULL"),
        br=_field(2, 2),
        ar=_field(3, 3),
    )
    with pytest.raises(ValueError, match="unsupported TD field status"):
        summarize_plate_candidate_impact(
            [row],
            {"trade_date": "20260930", "mapping": {"600000": "plate-A"}},
            trade_date="20260930",
        )
