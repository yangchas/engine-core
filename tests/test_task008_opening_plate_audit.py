from __future__ import annotations

import pytest
import hashlib
import json
from datetime import datetime
from pathlib import Path

from examples.audit_task008_real_opening_plate_amount import (
    _compare_plates,
    _load_captured_td_rows,
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


def test_captured_td_rows_require_matching_hash_and_restore_timestamp_type(tmp_path: Path):
    source = tmp_path / "td-rows.jsonl"
    source.write_text(
        json.dumps({"symbol": "000001", "ts": "2026-09-29T09:25:00+08:00"})
        + "\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256(source.read_bytes()).hexdigest()

    rows = _load_captured_td_rows(source, expected_sha256=digest)

    assert rows[0]["symbol"] == "000001"
    assert rows[0]["ts"] == datetime.fromisoformat("2026-09-29T09:25:00+08:00")
    with pytest.raises(ValueError, match="SHA-256"):
        _load_captured_td_rows(source, expected_sha256="0" * 64)
