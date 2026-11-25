"""design/33 S5: every way Core opens a TD connection reports an unreachable server as TD_UNAVAILABLE (retryable)."""
import importlib
import sys
import types

import pytest

from engine_core.data_pipeline.td_sink import TdUnavailable, connect_td
from engine_core.errors import is_retryable


@pytest.fixture
def unreachable_td(monkeypatch):
    # DATA: stand-in for a taos client whose server cannot be reached (no recorded response exists for a refused connect)
    def refuse(*_args, **_kwargs):
        raise RuntimeError("[0x000b]: Unable to establish connection")
    module = types.ModuleType("taos")
    module.connect = refuse
    monkeypatch.setitem(sys.modules, "taos", module)
    monkeypatch.setenv("TD_CONFIG", "/nonexistent/taos-cfg")


ENTRY_POINTS = [
    ("engine_core.data_pipeline.td_sink", lambda m: m.connect_td()),
    ("engine_core.plate_amount_baseline", None),
    ("engine_core.auction_baseline", lambda m: m._td_connect()),
    ("engine_core.guardian.td_resource_check", lambda m: m.connect()),
    ("engine_core.guardian.gates", lambda m: m._td()),
    ("engine_core.guardian.intraday_checkpoint_run", lambda m: m._td_connection()),
    ("engine_core.data_pipeline.providers.limit_pool_sources", lambda m: m.td_connect()),
    ("engine_core.guardian.startup", lambda m: m._td_probes()),
]


@pytest.mark.parametrize("module_name,call", ENTRY_POINTS, ids=[name for name, _ in ENTRY_POINTS])
def test_unreachable_td_is_td_unavailable_at_every_entry_point(unreachable_td, monkeypatch, tmp_path, module_name, call):
    module = importlib.import_module(module_name)
    if call is None:
        from engine_core import ladder_daily_adapter
        monkeypatch.setattr(ladder_daily_adapter, "trading_days", lambda *_: ["2026-11-05"])
        call = lambda m: m.build_and_store_plate_amount_baseline("2026-11-06", artifact_root=tmp_path, publish=False)
    with pytest.raises(TdUnavailable) as raised:
        call(module)
    assert raised.value.error_code == "TD_UNAVAILABLE" and raised.value.retryable and is_retryable("TD_UNAVAILABLE")


def test_missing_config_is_td_unavailable(monkeypatch):
    monkeypatch.delenv("TD_CONFIG", raising=False)
    with pytest.raises(TdUnavailable):
        connect_td()
