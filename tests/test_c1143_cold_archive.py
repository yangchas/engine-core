import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from engine_core.data_pipeline import cold_archive, hot_plates_gaps
from test_c1142_cold_archive import sample


def test_historical_td_cursor_contract(monkeypatch):
    raw, meta = sample()
    class Cursor:
        def __init__(self): self.sql = []; self.rows = []
        def execute(self, sql):
            self.sql.append(sql)
            if sql.startswith('INSERT INTO '):
                for number, count, digest, payload in re.findall(
                        r",(-?\d+),(\d+),'([0-9a-f]{64})','\\x([0-9a-f]*)'\)", sql):
                    item = (int(number), int(count), digest, bytes.fromhex(payload))
                    self.rows = [r for r in self.rows if r[0] != item[0]] + [item]
        def fetchall(self): return self.rows
        def close(self): pass
    class Connection:
        def __init__(self): self.cur = Cursor()
        def cursor(self): return self.cur
        def close(self): pass
    conn = Connection()
    from engine_core.data_pipeline import td_sink
    monkeypatch.setattr(td_sink, 'connect_td', lambda database: conn)
    day = '2000-01-01'
    cold_archive.TdStore().ensure('hot_plates_gaps', day, meta['version'])
    conn.cur.sql.clear()
    store = cold_archive.TDColdClient('core_data', connect=lambda: conn)
    store.ensure('hot_plates_gaps', day, meta['version'])
    cold_archive.write_envelope(store, 'hot_plates_gaps', day, meta['version'], raw, meta)
    sql = conn.cur.sql
    assert sql[0].startswith('CREATE STABLE IF NOT EXISTS core_data.')
    assert ' USING core_data.' in sql[1] and ' TAGS(' in sql[1]
    assert any(s.startswith('SELECT chunk_no,chunk_count,sha256,payload FROM core_data.') for s in sql)
    assert any("'\\x" + raw[:32].hex() in s for s in sql if s.startswith('INSERT INTO '))
    assert not any('KEEP' in s for s in sql)
    class MemoryStore:
        def __init__(self): self.rows = {}
        def read(self, *args): return list(self.rows.values())
        def write(self, dataset, day, version, rows, *, meta):
            self.rows.update((r['chunk_no'], dict(r)) for r in rows)
    memory = MemoryStore()
    cold_archive.write_envelope(memory, 'hot_plates_gaps', day, meta['version'], raw, meta)
    assert cold_archive.read_envelope('hot_plates_gaps', day, meta['version'], store=memory) == raw
    assert cold_archive.read_envelope('hot_plates_gaps', day, meta['version'], store=store) == raw


def test_publish_waits_for_archive_and_flush_timeout(tmp_path, monkeypatch, caplog):
    raw, row = sample()
    written = threading.Event()
    class Store:
        def __init__(self): self.rows = []
        def read(self, *args): return self.rows
        def write(self, dataset, day, version, rows, *, meta):
            time.sleep(0.03)
            self.rows.extend(rows)
            if rows[0]['chunk_no'] == -1: written.set()
        def close(self): pass
    worker = cold_archive.Archiver(Store)
    monkeypatch.setattr(cold_archive, '_worker', worker)
    monkeypatch.setattr(cold_archive, '_enabled', lambda: True)
    hot_plates_gaps._archive_gap(raw, row)
    assert written.is_set()
    stalled = cold_archive.Archiver(Store, start=False)
    assert stalled.submit('hot_plates_gaps', row['trade_date'], row['version'], raw, row)
    assert stalled.flush(0.01) is False
    assert 'cold_archive_flush_timeout' in caplog.text
    output = tmp_path / 'written'
    script = '''
import pathlib, sys, time
from engine_core.data_pipeline import cold_archive
raw = pathlib.Path(sys.argv[1]).read_bytes()
class Store:
    def read(self, *args): return []
    def write(self, dataset, day, version, rows, *, meta):
        time.sleep(0.05)
        if rows[0]['chunk_no'] == -1: pathlib.Path(sys.argv[2]).write_bytes(raw)
    def close(self): pass
cold_archive._worker = cold_archive.Archiver(Store)
cold_archive._worker.submit('hot_plates_gaps', '2000-01-01', 'v', raw, {})
'''
    fixture = Path(__file__).parent / 'fixtures/real/c1142/envelope.json'
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).parents[1] / 'src'))
    subprocess.run([sys.executable, '-c', script, str(fixture), str(output)], check=True, env=env)
    assert output.read_bytes() == raw
