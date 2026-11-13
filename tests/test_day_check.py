import json
from pathlib import Path

ROOT = Path(__file__).parent / 'fixtures'


def test_schema_real_old_and_replay():
    from examples.day_check import classify_schema
    old = json.loads((ROOT / 'real/redis_auction_full_row_2026-11-12_1207.json').read_text())['rows']
    new = json.loads((ROOT / 'real/t1v2_replay_row_2026-11-12_1207.json').read_text())['rows']
    assert classify_schema(old) == (0, 3)
    assert classify_schema(new) == (3, 3)


def test_ledger_real_failed_expected():
    from examples.day_check import ledger_states
    events = ledger_states((ROOT / 'day_check/ledger.jsonl').read_text().splitlines())
    assert events['startup_0830']['status'] == 'FAILED_FINAL'
    assert events['startup_0830']['exit_code'] == 40
    assert events['startup_0830']['expected'] is True


def test_report_real_manifest_missing_seal_is_unknown():
    from examples.day_check import report_summary
    manifest = json.loads((ROOT / 'day_check/manifest.json').read_text())
    result = report_summary(manifest, {})
    assert result['sources'] == {'0920': 'redis_auction_full', '0924': 'redis_auction_full', '0925': 'redis_auction_full'}
    assert result['seal_label'] is None and result['counts'] is None
