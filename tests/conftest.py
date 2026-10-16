"""Keep test helper imports stable from both repo and workspace roots."""

from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
project_root_text = str(PROJECT_ROOT)
if project_root_text not in sys.path:
    sys.path.insert(0, project_root_text)


import pytest


@pytest.fixture(autouse=True)
def _isolated_slice_root(tmp_path, monkeypatch):
    """Per-test slice/state cache root so past-dated fake TD states never leak between tests or into ~/core-data."""
    monkeypatch.setenv("CORE_SLICE_ROOT", str(tmp_path / "slices"))
    monkeypatch.setenv("CORE_DATA_ROOT", str(tmp_path / "core-data"))
    monkeypatch.setenv("CORE_STATE_CACHE", "off")  # cache tests opt in explicitly
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")  # tests must never read the real local Redis
