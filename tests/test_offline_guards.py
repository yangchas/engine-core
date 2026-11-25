# DATA: none (guard behaviour only)
"""Offline tests must not reach real upstream SDKs (conftest guard, C662c/d)."""
import importlib

import pytest


def test_real_kaipan_request_functions_are_blocked_but_config_reads_through():
    module = importlib.import_module("pykaipan.pykaipan")
    with pytest.raises(RuntimeError, match="offline test"):
        module.getHisBans(date="2026-10-23", ban="1", size="10")
    assert isinstance(module.config, dict)         # credentials lookups (fetch_kaipan_snapshot) keep working
