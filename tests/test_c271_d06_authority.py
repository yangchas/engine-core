from engine_core.daily_pipeline import _d06_persisted_assignments
from engine_core.slices import SliceResult


def test_d06_rejects_prepared_product_until_inputs_are_shared():
    product = SliceResult.create(
        [{"symbol": "000001", "trade_date": "2026-10-16",
          "used_date": "2026-10-15", "as_of": "2026-10-16T09:26:00+08:00",
          "input_version": "v1", "visibility_sha256": "same"}],
        {"status": "READY", "pit_date": "2026-10-15", "available_at": "2026-10-16T09:00:00+08:00"},
    )
    assert _d06_persisted_assignments(
        product, trade_date="2026-10-16", used_date="2026-10-15",
        asof="2026-10-16T09:26:00+08:00", input_version="v1",
        visibility_sha256="same",
    ) == {}
