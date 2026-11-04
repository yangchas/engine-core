# DATA: none (guard behaviour only)
"""Offline tests must not reach real upstream SDKs (conftest guard, C662c)."""
import importlib

import pytest


def test_real_kaipan_sdk_is_blocked_in_offline_tests():
    module = importlib.import_module("pykaipan.pykaipan")
    with pytest.raises(RuntimeError, match="offline test"):
        module.getHisBans(date="2026-10-23", ban="1", size="10")
