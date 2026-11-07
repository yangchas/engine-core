# Real ledger excerpt, 2026-11-06; source: /home/exedev/core-data/guardian-native/state/ledger/2026-11-06.jsonl
# Extraction: rg '"event_id":"td_resource_check_0825"' /home/exedev/core-data/guardian-native/state/ledger/2026-11-06.jsonl | head -n 3
from examples.pm import TERMINAL, latest_state, main

REAL_LINES = ['{"seq":136,"ts":"2026-11-06T08:25:00.004958743+08:00","type":"INSTANCE_STATE","crc32":1313832251,"data":{"event_id":"td_resource_check_0825","instance":"td_resource_check_0825","trade_date":"2026-11-06","state":{"status":"RUNNING","attempts":0,"started_at":"2026-11-06T08:25:00+08:00","completed_at":"","last_error":""}}}', '{"seq":139,"ts":"2026-11-06T08:25:00.50086676+08:00","type":"INSTANCE_STATE","crc32":264149152,"data":{"event_id":"td_resource_check_0825","instance":"td_resource_check_0825","trade_date":"2026-11-06","state":{"status":"RUNNING","attempts":1,"rung":1,"tier":"legacy","rung_attempts":1,"started_at":"2026-11-06T08:25:00+08:00","completed_at":"","last_error":"","data_date":"2026-11-06"}}}', '{"seq":143,"ts":"2026-11-06T08:25:02.474745556+08:00","type":"INSTANCE_STATE","crc32":2638055658,"data":{"event_id":"td_resource_check_0825","instance":"td_resource_check_0825","trade_date":"2026-11-06","state":{"status":"SUCCEEDED_DEGRADED","attempts":1,"rung":1,"tier":"legacy","rung_attempts":1,"started_at":"2026-11-06T08:25:00+08:00","completed_at":"2026-11-06T08:25:02+08:00","last_error":"","data_date":"2026-11-06"}}}']


def test_latest_state():
    event = 'td_resource_check_0825'
    running = latest_state(REAL_LINES[:2], event)
    assert running['status'] == 'RUNNING' and running['status'] not in TERMINAL
    assert latest_state(REAL_LINES, event)['status'] == 'SUCCEEDED_DEGRADED'
    assert latest_state(REAL_LINES, 'absent') is None


def test_wait_timeout(tmp_path, capsys):
    ledger = tmp_path / 'state/ledger/2026-11-06.jsonl'
    ledger.parent.mkdir(parents=True)
    ledger.write_text('\n'.join(REAL_LINES) + '\n')
    assert main(['wait', '--ledger-event', 'absent', '--date', '2026-11-06',
                 '--timeout', '1', '--interval', '1', '--state-root', str(tmp_path)]) == 2
    assert capsys.readouterr().out.startswith('TIMEOUT absent')
