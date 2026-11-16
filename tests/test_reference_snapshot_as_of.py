"""Replay read-only TD ACTIVE markers captured for C1287."""
import json
from pathlib import Path

import pytest

from engine_core.data_pipeline.reference_snapshot_td import ReferenceSnapshotStore

MARKERS = json.loads((Path(__file__).parent / 'fixtures/real/c1287/markers.json').read_text())


class Cursor:
    def __init__(self):
        self.sql = ''

    def execute(self, sql):
        self.sql = sql
        assert "snapshot_state='ACTIVE'" in sql and 'ORDER BY ts DESC' in sql

    def fetchall(self):
        return MARKERS

    def close(self):
        pass


class Connection:
    def cursor(self):
        return Cursor()

    def close(self):
        pass


def test_as_of_selects_real_active_markers(monkeypatch):
    store = ReferenceSnapshotStore('core_data', connect=Connection)
    class Selected(Exception):
        pass
    def rows(dataset, version):
        raise Selected(version)
    monkeypatch.setattr(store, '_rows_for_version', rows)
    for cutoff, expected in [
        ('2026-11-16T09:26:00+08:00', '20261116010016-dce913bc3963'),
        ('2026-11-16T15:30:00+08:00', '20261116070002-a97de1bbedc6'),
        ('2026-11-16T00:30:00+08:00', '20261113072013-f9c31b650349'),
    ]:
        with pytest.raises(Selected) as caught:
            store.read_as_of('plate_members', cutoff)
        assert str(caught.value) == expected
    assert store.read_as_of('plate_members', '2026-11-01T00:00:00+08:00') is None
    latest = object()
    monkeypatch.setattr(store, 'read_latest', lambda dataset: latest)
    assert store.read_as_of('plate_members', None) is latest
