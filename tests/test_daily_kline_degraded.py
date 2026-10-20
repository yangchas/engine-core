from engine_core.daily_kline_degraded import compose_degraded_rows


def test_degraded_rows_keep_field_provenance_and_partial_coverage():
    # Synthetic provider responses exercise merge semantics; this is not a historical fixture.
    rows, quality = compose_degraded_rows(
        [
            {
                "symbol": "000001",
                "date": "2026-10-20",
                "open": None,
                "high": 11.6,
                "low": 11.3,
                "close": 11.44,
                "pre_close": 11.65,
                "amount_yuan": 1429559936,
                "volume": 1245097,
                "source": "legacy_q2_final",
            }
        ],
        [
            {
                "symbol": "000001",
                "date": "2026-10-20",
                "open": 11.48,
                "high": 11.6,
                "low": 11.3,
                "close": 11.44,
                "pre_close": 11.65,
                "amount_yuan": 1429559900,
                "volume": 1245097,
                "source": "wencai",
            },
            {
                "symbol": "000002",
                "date": "2026-10-20",
                "open": 3.0,
                "high": 3.1,
                "low": 2.9,
                "close": 3.05,
                "source": "wencai",
            },
        ],
        expected_symbols=["000001", "000002", "000003"],
    )

    assert len(rows) == 2
    assert rows[0]["symbol"] == "000001"
    assert rows[0]["open"] == 11.48
    assert rows[0]["close"] == 11.44
    assert rows[0]["field_sources"]["open"] == "wencai"
    assert rows[0]["field_sources"]["close"] == "q2_final"
    assert rows[0]["degraded"] is True
    assert quality == {
        "status": "PARTIAL",
        "coverage": 2 / 3,
        "row_count": 2,
        "expected_count": 3,
        "missing": 1,
        "missing_symbols": ["000003"],
    }
