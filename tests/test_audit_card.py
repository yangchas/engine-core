# DATA: spec (temporary git repo built in the test)
import subprocess
import sys

from examples.audit_card import check_scope, count_new_tests, fail_first, verdict


def test_scope():
    assert check_scope(['examples/a.py', 'other.py'], ['/repo/examples/a.py'], '/repo') == ['other.py']


def test_count_new_tests():
    assert count_new_tests({'tests/a.py': 'def test_old(): pass\n'}, {'tests/a.py': 'def test_old(): pass\ndef test_new(): pass\n', 'src/a.py': 'def test_no(): pass\n'}) == 1


def test_fail_first(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    def git(*args):
        return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()
    git('init', '-q')
    git('config', 'user.email', 'test@example.invalid')
    git('config', 'user.name', 'Test')
    (repo / 'src_mod.py').write_text('def value(): return 0\n')
    git('add', '.')
    git('commit', '-qm', 'base')
    base = git('rev-parse', 'HEAD')
    (repo / 'tests').mkdir()
    (repo / 'tests/test_value.py').write_text('from src_mod import value\ndef test_value(): assert value() == 1\n')
    (repo / 'src_mod.py').write_text('def value(): return 1\n')
    git('add', '.')
    git('commit', '-qm', 'fix')
    failed, output = fail_first(str(repo), base, 'HEAD', ['tests/test_value.py'], sys.executable)
    assert failed and 'failed' in output
    result = subprocess.run([sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider', 'tests/test_value.py'], cwd=repo, env={'PATH': '/usr/bin:/bin', 'PYTHONPATH': '.', 'TMPDIR': '/tmp'}, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_verdict():
    assert verdict(['bad']) == {'verdict': 'FAIL', 'problems': ['bad']}
