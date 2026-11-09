from engine_core.guardian.daily_kline_coverage import day_coverage_ratio
from engine_core.data_pipeline.registry.daily_kline import DAILY_KLINE_QUALITY_FIELDS

DAY = "2026-10-29"
SYMBOLS = [f"{n:06d}" for n in range(5224)]


class Store:
    def __init__(self, count, provider="baostock", status="FORMAL", needs_upgrade=False):
        self.rows = {symbol: {"version": "v1"} for symbol in SYMBOLS[:count]}
        self.provider = provider
        self.status = status
        self.needs_upgrade = needs_upgrade
        self.reads = 0

    def schema_exists(self):
        return True

    def read_versions(self, day):
        return [{"version": "v1", "provider": self.provider, "business_status": self.status,
                 "needs_upgrade": self.needs_upgrade,
                 "quality": {"source_dates_consistent": True, "source_date_values": [day],
                             "source_dates": {field: day for field in DAILY_KLINE_QUALITY_FIELDS}}}]

    def read_day_rows(self, day):
        self.reads += 1
        return self.rows


def test_ready_coverage_and_missing():
    store = Store(5218)
    result = day_coverage_ratio(store, DAY, SYMBOLS)
    assert result == {"coverage": 5218 / 5224, "expected": 5224, "present": 5218,
                      "missing_symbols": SYMBOLS[5218:], "missing_total": 6,
                      "provider": "baostock", "version": "v1", "ready": True, "reason": ""}
    assert store.reads == 1


def test_provisional_version_is_not_ready_despite_coverage():
    result = day_coverage_ratio(Store(5218, status="PROVISIONAL"), DAY, SYMBOLS)
    assert result["coverage"] == 5218 / 5224 and result["present"] == 5218
    assert result["provider"] == "baostock" and result["version"] == "v1"
    assert result["missing_total"] == 6 and result["missing_symbols"] == SYMBOLS[5218:]
    assert not result["ready"] and result["reason"] == "not_formal:provisional"


def test_formal_version_needing_upgrade_is_not_ready():
    result = day_coverage_ratio(Store(5218, needs_upgrade=True), DAY, SYMBOLS)
    assert result["coverage"] == 5218 / 5224 and result["present"] == 5218
    assert result["provider"] == "baostock" and result["version"] == "v1"
    assert result["missing_total"] == 6 and result["missing_symbols"] == SYMBOLS[5218:]
    assert not result["ready"] and result["reason"] == "needs_upgrade"


def test_below_threshold():
    result = day_coverage_ratio(Store(418), DAY, SYMBOLS)
    assert result["coverage"] == 418 / 5224
    assert result["missing_total"] == 4806
    assert result["missing_symbols"] == SYMBOLS[418:468]
    assert not result["ready"] and result["reason"] == "below_threshold"


def test_other_provider():
    result = day_coverage_ratio(Store(5218, "other"), DAY, SYMBOLS)
    assert not result["ready"] and result["reason"] == "non_baostock_provider"


def test_source_unavailable_excluded():
    result = day_coverage_ratio(Store(5218), DAY, SYMBOLS, source_unavailable=SYMBOLS[5218:])
    assert result["expected"] == result["present"] == 5218
    assert result["coverage"] == 1 and result["missing_total"] == 0 and result["ready"]


def test_td_unavailable():
    class Down(Store):
        def schema_exists(self):
            raise RuntimeError("TD down")
    result = day_coverage_ratio(Down(0), DAY, SYMBOLS)
    assert result["coverage"] is None and result["present"] is None
    assert not result["ready"] and result["reason"] == "td_unavailable"
