import hashlib
import json
import re
from pathlib import Path

from engine_core.data_pipeline import cold_archive as ca

RAW = (Path(__file__).parent / 'fixtures/real/c1245/checkpoint.json').read_bytes()


def test_real_checkpoint_is_batched_and_roundtrips(monkeypatch):
    assert ca.MAX_INSERT_SQL_BYTES <= 500_000
    provenance = json.loads((Path(__file__).parent / 'fixtures/real/PROVENANCE.json').read_text())['_c1245']['c1245/checkpoint.json']
    assert hashlib.sha256(RAW).hexdigest() == provenance['sha256']
    monkeypatch.setattr(ca, 'MAX_INSERT_SQL_BYTES', 25000, raising=False)
    client = ca.TDColdClient(connect=lambda: None)
    sqls = []
    rows = {}
    def execute(sql, fetch=False):
        if fetch:
            return list(rows.values())
        if sql.startswith('INSERT'):
            sqls.append(sql)
            assert len(sql.encode()) <= ca.MAX_INSERT_SQL_BYTES
            for no, count, digest, payload in re.findall(r",(-?\d+),(\d+),'([0-9a-f]+)','\\x([0-9a-f]*)'\)", sql):
                rows[int(no)] = (int(no), int(count), digest, bytes.fromhex(payload))
    monkeypatch.setattr(client, '_execute', execute)
    version = '09:33:00:sha256-' + hashlib.sha256(RAW).hexdigest()[:16]
    ca.write_envelope(client, 'intraday_checkpoint', '2026-11-16', version, RAW, json.loads(RAW)['manifest'])
    assert len(sqls) > 2
    assert ca.read_envelope('intraday_checkpoint', '2026-11-16', version, store=client) == RAW


def test_failed_td_batch_reports_error_and_partial_count(monkeypatch):
    monkeypatch.setattr(ca, 'MAX_INSERT_SQL_BYTES', 25000, raising=False)
    class Client(ca.TDColdClient):
        def __init__(self): self.database = 'core_data'; self.writes = 0
        def ensure(self, *args): pass
        def read(self, *args): return []
        def _execute(self, sql, fetch=False):
            self.writes += 1
            if self.writes == 2: raise RuntimeError('[0x0219]: SQL statement too long')
        def close(self): pass
    worker = ca.Archiver(Client)
    receipt = worker.submit('intraday_checkpoint', '2026-11-16', 'test', RAW, {})
    assert worker.flush(5) is False
    assert receipt.success is False
    assert receipt.successful_chunks > 0
    assert receipt.error_category == 'RuntimeError'
    assert receipt.td_error_code == '0x0219'
