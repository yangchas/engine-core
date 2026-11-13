import subprocess
from pathlib import Path

from examples.test_audit import audit

REPO = Path(__file__).resolve().parents[1]


def historical_repo(tmp_path, commit):
    tmp_path.mkdir(parents=True, exist_ok=True)
    repo = tmp_path / 'history'
    subprocess.run(['git', 'clone', '--shared', '--no-checkout', '-q', str(REPO), str(repo)], check=True)
    subprocess.run(['git', '-C', str(repo), 'checkout', '--detach', '-q', commit], check=True)
    return repo


def test_new_tests_count_real_history(tmp_path):
    repo = historical_repo(tmp_path, '61d7fcf')
    diff = subprocess.check_output(['git', '-C', str(repo), 'show', '--format=', '--unified=0', '61d7fcf', '--', 'tests/test_pm_guard.py'], text=True)
    independent = sum(1 for line in diff.splitlines() if line.startswith('+def test_'))
    assert independent == 3
    result = audit(repo, '061899e', 'TEST_BUDGET: 新增测试 ≤3')
    assert any(f'new_tests: {independent}' in line for line in result)
    assert not any('test_budget_exceeded' in line for line in result)


def test_real_handwritten_and_fixture_controls(tmp_path):
    for commit, expected in [('8590fa0', True), ('6fadd2d', False)]:
        repo = historical_repo(tmp_path / commit, commit)
        result = audit(repo, f'{commit}^', '')
        assert any('handwritten_market_rows' in line for line in result) == expected


def test_real_budget_exceeded(tmp_path):
    repo = historical_repo(tmp_path, '6fadd2d')
    assert any(line.startswith('FAIL test_budget_exceeded:') for line in audit(repo, '6fadd2d^', 'TEST_BUDGET: 新增测试 ≤1'))
