import asyncio
import importlib.util
from dataclasses import dataclass
from pathlib import Path


spec = importlib.util.spec_from_file_location(
    "reference_probe",
    Path(__file__).parents[1] / "examples" / "run_real_reference_probe.py",
)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


@dataclass(frozen=True)
class DailyRow:
    trade_date: str
    symbol: str
    close: float
    amount: float


class Bao:
    def fetch_daily_kline(self, request):
        return [DailyRow(request.trade_date, request.symbol, 10.0, 100.0)]


class Kai:
    def fetch_hot_plates(self, trade_date):
        return [{"name": "人工智能", "rank": 1}]

    def fetch_yesterday_bans_pool(self, trade_date, max_ban=3):
        return [{"code": "000001", "name": "平安银行", "lb_days": 1}]

    def fetch_ban_reasons(self, symbol):
        return [{"symbol": symbol, "reason": "示例", "source_trade_date": None}]


class Wen:
    async def fetch_limitup_with_lb_days(self, max_stocks=10):
        await asyncio.sleep(0)
        return object()

    def normalize_limitup_with_lb_days(self, frame):
        return [{"symbol": "000001", "lb_days": 1, "source": "wencai"}]


class Ths:
    async def fetch_hot_rank(self, top_n=10):
        await asyncio.sleep(0)
        return [{"code": "SZ000001"}]

    def normalize_hot_rank(self, rows):
        return [{"symbol": "000001", "rank": 1, "source": "ths_hot_rank"}]


@dataclass(frozen=True)
class Request:
    symbol: str
    trade_date: str


def test_probe_separates_connectivity_from_historical_date_authority():
    result = probe.probe_sources(
        {"baostock": Bao(), "kaipan": Kai(), "wencai": Wen(), "ths": Ths()},
        daily_request_factory=Request,
        trade_date="2026-09-08",
        symbol="600000",
        max_rows=10,
    )
    assert result["connection_pass_count"] == result["connection_total_count"] == 6
    assert result["observations"]["baostock_daily_kline"]["contract_status"] == "PASS"
    assert result["observations"]["wencai_limit_truth"]["date_semantics"] == (
        "CURRENT_QUERY_NO_STRUCTURED_DATE"
    )
    assert result["observations"]["wencai_limit_truth"]["historical_runtime_role"] == (
        "UNAVAILABLE_WITHOUT_DATED_QUERY_EVIDENCE"
    )
    assert result["observations"]["ths_hot_rank"]["historical_runtime_role"] == "UNAVAILABLE"
    assert result["observations"]["kaipan_ban_reasons"]["contract_status"] == "OBSERVED"


def test_baostock_date_mismatch_is_contract_failure_not_connection_failure():
    class WrongDateBao(Bao):
        def fetch_daily_kline(self, request):
            return [DailyRow("2026-09-07", request.symbol, 10.0, 100.0)]

    result = probe.probe_sources(
        {"baostock": WrongDateBao(), "kaipan": Kai(), "wencai": Wen(), "ths": Ths()},
        daily_request_factory=Request,
        trade_date="2026-09-08",
        symbol="600000",
        max_rows=10,
    )
    observation = result["observations"]["baostock_daily_kline"]
    assert observation["connection_status"] == "PASS"
    assert observation["contract_status"] == "FAIL"
    assert observation["date_semantics"] == "DATE_MISMATCH"
