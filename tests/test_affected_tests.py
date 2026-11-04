# DATA: real:repository git history (the last commits are the ground truth; skipped when the tree has no .git)
"""C666: the selector must cover every test a commit changed, given only that commit's non-test changes."""
import subprocess
from pathlib import Path

import pytest

from examples import affected_tests

ROOT = Path(__file__).resolve().parents[1]


def _git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def _recent_commits(count=10):
    try:
        shas = _git("log", "--no-merges", "--format=%H", "-n", "60", "--", "tests").split()
    except Exception:
        return []
    picked = []
    for sha in shas:
        files = _git("show", "--name-only", "--format=", sha).split()
        tests = [f for f in files if f.startswith("tests/test_") and f.endswith(".py")]
        rest = [f for f in files if not f.startswith("tests/test_")]
        existing = [t for t in tests if (ROOT / t).exists()]       # later deleted/renamed tests cannot be selected
        if existing and rest:
            picked.append((sha[:8], existing, rest))
        if len(picked) == count:
            break
    return picked


COMMITS = _recent_commits()


@pytest.mark.skipif(not COMMITS, reason="no git history in this tree")
@pytest.mark.parametrize("depth", [None, 2], ids=["full", "default-depth-2"])
@pytest.mark.parametrize("sha,tests,rest", COMMITS, ids=[c[0] for c in COMMITS])
def test_selector_covers_the_tests_each_commit_changed(sha, tests, rest, depth):
    selected = set(affected_tests.affected_tests(rest, depth=depth))
    assert set(tests) <= selected, sorted(set(tests) - selected)


def test_import_closure_follows_imports_transitively():
    graph = {"a": {"b"}, "b": {"c"}, "c": set(), "d": set()}
    assert affected_tests.import_closure(["a"], graph) == {"a", "b", "c"}


def test_conftest_change_selects_every_test():
    everything = affected_tests.affected_tests(["tests/conftest.py"])
    assert "tests/test_affected_tests.py" in everything and len(everything) > 100
