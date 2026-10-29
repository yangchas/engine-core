from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples"))

import accept_c618  # noqa: E402


def test_staging_probe_uses_bounded_row_lookup(monkeypatch):
    queries = []

    class Cursor:
        last_query = ""

        def execute(self, query):
            self.last_query = query
            queries.append(query)

        def fetchone(self):
            if "FROM reference_snapshot_versions" in self.last_query:
                return (datetime.fromtimestamp(2_000, tz=timezone.utc), "20261029230000-abcdef123456", 89943,
                        "a" * 64)
            if "FROM plate_members_latest" in self.last_query:
                return ("row-id",)
            pytest.fail(f"unexpected query: {self.last_query}")

    class Connection:
        def __init__(self):
            self.closed = False

        def cursor(self):
            return Cursor()

        def close(self):
            self.closed = True

    connection = Connection()

    monkeypatch.setattr(accept_c618, "_tables",
                        lambda database: pytest.fail("staging probe scanned the full table catalog"))
    monkeypatch.setattr(accept_c618, "_connect", lambda database: connection)

    staged = accept_c618._staging_after("core_dev_test", 1_000)

    assert staged["staged_row_present"] is True
    assert len(queries) == 2
    assert "LIMIT 1" in queries[1]
    assert "COUNT" not in queries[1]
    assert connection.closed
