from __future__ import annotations

import pytest

from examples.audit_task008_real_opening_plate_amount import (
    _compare_plates,
    _td_connection_settings,
)


def test_plate_parity_rejects_duplicate_legacy_plate_rows():
    with pytest.raises(ValueError, match="legacy.*unique"):
        _compare_plates(
            [{"plate": "AI"}, {"plate": "AI"}],
            [{"plate": "AI"}],
        )


def test_plate_parity_rejects_duplicate_core_plate_rows():
    with pytest.raises(ValueError, match="Core.*unique"):
        _compare_plates(
            [{"plate": "AI"}],
            [{"plate": "AI"}, {"plate": "AI"}],
        )


def test_plate_parity_rejects_blank_plate_ids_instead_of_coalescing_them():
    with pytest.raises(ValueError, match="non-empty"):
        _compare_plates([{"plate": ""}], [{"plate": ""}])


def test_td_connection_settings_require_explicit_endpoint_and_credentials():
    with pytest.raises(ValueError, match="TDENGINE_HOST"):
        _td_connection_settings({})


def test_td_connection_settings_validate_port_without_exposing_password():
    env = {
        "TDENGINE_HOST": "127.0.0.1",
        "TDENGINE_PORT": "not-a-port",
        "TDENGINE_USER": "readonly",
        "TDENGINE_PASSWORD": "sensitive-value",
    }
    with pytest.raises(ValueError) as exc_info:
        _td_connection_settings(env)
    assert "sensitive-value" not in str(exc_info.value)
