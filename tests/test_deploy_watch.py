# DATA: spec (ledger snapshot shape from live ~/core-data/guardian-native/state/ledger)
from datetime import datetime

from examples.deploy_watch import evaluate

DEPLOY = datetime.fromisoformat('2026-11-06T09:00:00+08:00')


def check(today, prior, **overrides):
    args = dict(deploy_at=DEPLOY, live_sha='abc', expected_sha='abc', heartbeat={'config_version': 1},
                expected_config_version=1, today_events=today, prior_events=prior, guardiand_running=True)
    args.update(overrides)
    return evaluate(**args)


def test_new_failure():
    result = check({'job': {'status': 'FAILED_FINAL', 'completed_at': '2026-11-06T09:01:00+08:00'}},
                   {'job': {'status': 'SUCCEEDED'}})
    assert result['verdict'] == 'RED'
    assert result['new_failures'] == ['job']


def test_chronic_failure():
    result = check({'job': {'status': 'FAILED_FINAL', 'completed_at': '2026-11-06T09:01:00+08:00'}},
                   {'job': {'status': 'FAILED_FINAL'}})
    assert result['verdict'] == 'GREEN'
    assert result['new_failures'] == []


def test_health_and_wait():
    assert check({}, {}, live_sha='wrong')['verdict'] == 'RED'
    assert check({}, {}, guardiand_running=False)['verdict'] == 'RED'
    assert check({}, {}, heartbeat={'config_version': 2})['verdict'] == 'RED'
    assert check({'job': {'status': 'RUNNING', 'started_at': '2026-11-06T09:01:00+08:00'}}, {})['verdict'] == 'WAIT'
