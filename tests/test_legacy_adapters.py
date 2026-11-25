import ast
import sys
import types
from pathlib import Path

import pandas as pd

from examples.legacy_adapters import (
    kaipan_analyzer,
    kaipan_plate_members_callable,
    kaipan_pykaipan_module,
    wencai_fetch_callable,
)


def test_wencai_adapter_adds_root_once_and_exposes_cookie_substrings(monkeypatch, tmp_path):
    calls = []
    fetcher_module = types.ModuleType("ai.API.api")

    class FakeFetcher:
        wencai_cookie = "abc-secret-xyz"

        async def _get_wencai_stocks(self, query, *, loop, max_stocks, return_df):
            calls.append((query, loop, max_stocks, return_df))
            return pd.DataFrame({"代码": ["000001"]})

    fetcher_module.UnifiedMarketDataFetcher = FakeFetcher
    package = types.ModuleType("ai")
    api = types.ModuleType("ai.API")
    monkeypatch.setitem(sys.modules, "ai", package)
    monkeypatch.setitem(sys.modules, "ai.API", api)
    monkeypatch.setitem(sys.modules, "ai.API.api", fetcher_module)
    root = str(tmp_path)
    before = sys.path.count(root)

    fetch = wencai_fetch_callable(root)
    frame = fetch("涨停", True, 25)

    assert isinstance(frame, pd.DataFrame)
    assert calls == [("涨停", True, 25, True)]
    assert sys.path.count(root) == before + 1
    assert {"abc", "secret", "xyz", "abc-secret-xyz"}.issubset(fetch.redaction_values())


def test_kaipan_adapters_import_lazily(monkeypatch):
    analyzer_module = types.ModuleType("ai.API.StockAnalyzer")
    expected_analyzer = object()
    analyzer_module.StockAnalyzer = lambda: expected_analyzer
    package = types.ModuleType("ai")
    api = types.ModuleType("ai.API")
    kaipan_module = types.ModuleType("pykaipan.pykaipan")
    monkeypatch.setitem(sys.modules, "ai", package)
    monkeypatch.setitem(sys.modules, "ai.API", api)
    monkeypatch.setitem(sys.modules, "ai.API.StockAnalyzer", analyzer_module)
    monkeypatch.setitem(sys.modules, "pykaipan", types.ModuleType("pykaipan"))
    monkeypatch.setitem(sys.modules, "pykaipan.pykaipan", kaipan_module)

    assert kaipan_analyzer("/unused/legacy-root") is expected_analyzer
    assert kaipan_pykaipan_module() is kaipan_module


def test_kaipan_plate_members_callable_uses_fixed_point_in_time_arguments(monkeypatch, tmp_path):
    calls = []
    kaipan_module = types.ModuleType("pykaipan.pykaipan")
    kaipan_module.config = {"Token": "secret-token", "DeviceID": "device-id"}
    kaipan_module.getHisPlateIds = lambda **kwargs: calls.append(kwargs) or {"list": []}
    package = types.ModuleType("pykaipan")
    monkeypatch.setitem(sys.modules, "pykaipan", package)
    monkeypatch.setitem(sys.modules, "pykaipan.pykaipan", kaipan_module)

    fetch = kaipan_plate_members_callable(tmp_path)
    response = fetch("801159", "2026-10-13")

    assert response == {"list": []}
    assert calls == [{"p": "801159", "date": "2026-10-13", "index": "0", "size": "2000"}]
    assert fetch.redaction_values() == {"secret-token", "device-id"}


def test_engine_core_imports_no_legacy_or_examples_modules():
    source_root = Path(__file__).parents[1] / "src" / "engine_core"
    forbidden = {"examples", "ai.API", "engine_next", "pykaipan"}
    found = []
    for path in source_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                if any(name == prefix or name.startswith(prefix + ".") for prefix in forbidden):
                    found.append((path.relative_to(source_root).as_posix(), name))
    assert found == []


def test_package_import_does_not_pull_numpy():
    import subprocess
    import sys

    code = "import sys; sys.path.insert(0, 'src'); import engine_core; print('numpy' in sys.modules)"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout.strip()
    assert out == "False"
