from datetime import datetime, timezone
import importlib.util
from pathlib import Path


SPEC = importlib.util.spec_from_file_location(
    "real_opening_engine_shadow",
    Path(__file__).parents[1] / "examples" / "run_real_opening_engine_shadow.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class _FakeRedis:
    def smembers(self, key):
        return {b"600519"}

    def hgetall(self, key):
        return {
            b"mk": b"SH",
            b"px": b"10500",
            b"pc": b"10000",
            b"amt": b"1200000",
            b"vol": b"100",
            b"ts": b"1000",
            b"amt2m": b"50000",
            b"ls": b"1",
        }


def test_real_opening_path_runs_q2_adapter_through_public_engine():
    result = MODULE.run_real_opening_engine_shadow(
        client=_FakeRedis(),
        trade_date="1970-01-01",
        symbol="600519",
        observed_at=datetime.fromtimestamp(1000, tz=timezone.utc),
        stale_after_ms=100000000000,
    )

    assert result["read_only"] is True
    assert result["processed_signals"] == 2
    assert result["strategy_result"].trace["decision_status"] == "FACT_ONLY"
    assert result["strategy_result"].trace["fact_status"] in {"READY", "PARTIAL"}
