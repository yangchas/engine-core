# DATA: none - behaviour of the release tool on throw-away git repositories; the live checkout is never touched
"""C693: a release is one evidence-gated, reversible step; the tool refuses whenever it could not be undone."""
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples"))

import release  # noqa: E402

NOW = datetime(2026, 11, 5, 23, 0, 0)


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def _commit(repo, files, message):
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repos(tmp_path, monkeypatch):
    monkeypatch.setattr(release, "SMOKE_MODULES", ())
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    schedule = {"events": [{"event_id": "e_noon", "at": "12:00:00"}]}
    base = _commit(origin, {"src/engine_core/guardian/schedule.json": json.dumps(schedule), "a.py": "1\n",
                            "examples/affected_tests.py": "print('python -m pytest -q tests/test_a.py')\n"}, "base")
    live = tmp_path / "live"                                  # like production: a detached worktree of the same repository
    _git(origin, "worktree", "add", "-q", "--detach", str(live), base)
    head = _commit(origin, {"a.py": "2\n", "tests/test_a.py": "x\n"}, "Merge C900 (a python change)")
    evidence = tmp_path / "evidence"
    return origin, live, evidence, base, head


def _plan(origin, live, evidence, ps=None, now=NOW, errors=None):
    return release.build_plan(live, origin, "main", evidence, now, ps if ps is not None else [],
                              errors_path=errors or evidence / "no-errors.jsonl")


def _accept(evidence, commit, verdict="PASS"):
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / f"accept-{commit[:12]}.json").write_text(json.dumps({"commit": commit[:12], "verdict": verdict}))


def test_changes_are_classified_by_what_they_need():
    kinds = release.classify(["src/engine_core/a.py", "examples/b.py", "src/engine_core/guardian/schedule.json",
                              "native/guardiand/runtime.go", "tests/t.py", "native/guardiand/testdata/schedule.json"])
    assert kinds["python"] == ["src/engine_core/a.py", "examples/b.py"]
    assert kinds["config"] == ["src/engine_core/guardian/schedule.json", "native/guardiand/testdata/schedule.json"]
    assert kinds["go"] == ["native/guardiand/runtime.go"] and kinds["tests_docs"] == ["tests/t.py"]


def test_the_plan_lists_what_is_merged_and_not_live_and_is_blocked_without_acceptance(repos):
    origin, live, evidence, base, head = repos
    plan = _plan(origin, live, evidence)
    assert plan["merges"] == [f"{head[:7]} Merge C900 (a python change)"] and not plan["ready"]
    assert plan["files"]["python"] == 1 and plan["blockers"] == [
        "no PASS acceptance record for the candidate (run: release.py accept)"]


def test_a_failed_acceptance_blocks(repos):
    origin, live, evidence, base, head = repos
    _accept(evidence, head, "FAIL")
    assert not _plan(origin, live, evidence)["ready"]


@pytest.mark.parametrize("what", ["dirty", "handler", "event_due"])
def test_it_refuses_while_something_could_break_or_cannot_be_undone(repos, what):
    origin, live, evidence, base, head = repos
    _accept(evidence, head)
    ps, now = [], NOW
    if what == "dirty":
        (live / "a.py").write_text("local edit\n")
    elif what == "handler":
        ps = [f"123 python {live}/examples/guardian_handler.py run --handler baostock_probe"]
    else:
        now = datetime(2026, 11, 5, 11, 59, 0)             # e_noon is due in 60 s
    plan = _plan(origin, live, evidence, ps, now)
    assert not plan["ready"] and len(plan["blockers"]) == 1


def test_untracked_runtime_state_in_the_live_checkout_does_not_block(repos):
    origin, live, evidence, base, head = repos
    _accept(evidence, head)
    (live / ".mail_state").mkdir()
    (live / ".mail_state" / "x").write_text("state")
    assert _plan(origin, live, evidence)["ready"]


def test_only_handlers_of_the_live_checkout_count():
    other = ["9 python /home/x/other/examples/guardian_handler.py run --handler a",
             "8 python /live/examples/guardian_handler.py gate",
             "7 python /live/examples/guardian_handler.py run --handler b"]
    assert release.running_handlers(Path("/live"), other) == [other[2]]


def _args(origin, live, evidence, **extra):
    import argparse
    values = dict(live=live, candidate="main", evidence_dir=evidence, go=True, authorized_by="部署", date="2026-11-05",
                  work_dir=None, errors_file=evidence / "no-errors.jsonl")
    values.update(extra)
    return argparse.Namespace(**values)


def test_apply_moves_the_live_checkout_records_it_and_rollback_undoes_it(repos, monkeypatch):
    origin, live, evidence, base, head = repos
    monkeypatch.setattr(release, "ROOT", origin)
    monkeypatch.setattr(release, "running_handlers", lambda live, ps=None: [])
    _accept(evidence, head)
    assert release.cmd_apply(_args(origin, live, evidence)) == 0
    assert _git(live, "rev-parse", "HEAD") == head
    record = json.loads(next(evidence.glob("deploy-*.json")).read_text())
    assert record["previous"] == base and record["deployed"] == head and record["authorized_by"] == "部署"
    assert release.cmd_rollback(_args(origin, live, evidence)) == 0
    assert _git(live, "rev-parse", "HEAD") == base


def test_apply_and_rollback_need_the_go_word(repos):
    origin, live, evidence, base, head = repos
    for command in (release.cmd_apply, release.cmd_rollback):
        with pytest.raises(SystemExit):
            command(_args(origin, live, evidence, go=False))
        with pytest.raises(SystemExit):
            command(_args(origin, live, evidence, authorized_by=""))
    assert _git(live, "rev-parse", "HEAD") == base


def test_a_broken_import_smoke_restores_the_previous_commit(repos, monkeypatch):
    origin, live, evidence, base, head = repos
    monkeypatch.setattr(release, "ROOT", origin)
    monkeypatch.setattr(release, "running_handlers", lambda live, ps=None: [])
    monkeypatch.setattr(release, "SMOKE_MODULES", ("module_that_does_not_exist_c693",))
    _accept(evidence, head)
    with pytest.raises(SystemExit):
        release.cmd_apply(_args(origin, live, evidence))
    assert _git(live, "rev-parse", "HEAD") == base and not list(evidence.glob("deploy-*.json"))


def test_judge_flags_answers_outside_the_contract_and_unclassified_ones():
    outcomes = [
        {"event": "a", "exit": 0, "problems": [], "reply": {"status": "ok", "error_code": ""}},
        {"event": "b", "exit": 20, "problems": ["exit_20_requires_not_applicable_and_ALREADY_SATISFIED"],
         "reply": {"status": "failed", "error_code": "X"}},
        {"event": "c", "exit": 75, "problems": [], "reply": {"status": "failed", "error_code": "INTERNAL_ERROR"}},
        {"event": "d", "exit": 75, "problems": [], "reply": {"status": "failed", "error_code": "REDIS_UNAVAILABLE"}},
        {"event": "e", "exit": 1, "problems": [], "reply": None}]
    summary, outside, unclassified = release.judge(outcomes, {"d": ["redis.get", "redis.get"]})
    assert [row["event"] for row in outside] == ["b", "e"] and [row["event"] for row in unclassified] == ["c"]
    assert summary[3]["gaps"] == ["redis.get"]


# ---- the two-errors rule and the checked merge --------------------------------------------------------------------
def _errors(path, count, reviewed=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [{"at": "2026-11-06T01:00:00", "kind": "k", "note": f"n{i}", "impact": "", "retro": "r1" if i < reviewed else None}
            for i in range(count)]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_two_unreviewed_errors_block_the_release(repos):
    origin, live, evidence, base, head = repos
    _accept(evidence, head)
    log = evidence / "errors.jsonl"
    _errors(log, 1)
    assert _plan(origin, live, evidence, errors=log)["ready"]
    _errors(log, 2)
    plan = _plan(origin, live, evidence, errors=log)
    assert not plan["ready"] and "2 unreviewed errors" in plan["blockers"][0]
    _errors(log, 2, reviewed=2)                                  # a retrospective was held: they no longer count
    assert _plan(origin, live, evidence, errors=log)["ready"]


def _errors_args(log, *rest, **extra):
    import argparse
    values = dict(rest=list(rest), errors_file=log, kind=None, note=None, impact="", retro=None)
    values.update(extra)
    return argparse.Namespace(**values)


def test_errors_are_logged_counted_and_closed_by_a_retrospective(tmp_path, capsys):
    log = tmp_path / "retros" / "errors.jsonl"
    assert release.cmd_errors(_errors_args(log, "add", kind="process", note="first")) == 0
    assert release.cmd_errors(_errors_args(log, "add", kind="evidence", note="second")) == 1       # review due now
    assert "REVIEW DUE" in capsys.readouterr().out
    assert release.cmd_errors(_errors_args(log, "close", retro="2026-11-06.md")) == 0
    assert [e["retro"] for e in release.load_errors(log)] == ["2026-11-06.md", "2026-11-06.md"]
    with pytest.raises(SystemExit):
        release.cmd_errors(_errors_args(log, "add", kind="only-kind"))


@pytest.fixture
def mergeable(repos, monkeypatch, tmp_path):
    origin, live, evidence, base, head = repos
    _git(origin, "checkout", "-q", "-b", "feature", base)
    _commit(origin, {"b.py": "3\n"}, "a change")
    _git(origin, "checkout", "-q", "main")
    cards = tmp_path / "cards"
    cards.mkdir()
    monkeypatch.setattr(release, "ROOT", origin)
    for name in ("AUTHOR", "COMMITTER"):                          # the throw-away repository has no git identity
        monkeypatch.setenv(f"GIT_{name}_NAME", "t")
        monkeypatch.setenv(f"GIT_{name}_EMAIL", "t@t")
    return origin, evidence, cards


def _merge_args(origin, evidence, cards, **extra):
    import argparse
    values = dict(branch="feature", card="C900", message="a change", candidate="main", evidence_dir=evidence,
                  cards_dir=cards, errors_file=evidence / "no-errors.jsonl")
    values.update(extra)
    return argparse.Namespace(**values)


def test_merge_needs_the_card_and_passing_tests_and_records_the_card_in_the_subject(mergeable, monkeypatch):
    origin, evidence, cards = mergeable
    monkeypatch.setattr(release, "run_checked", lambda *a, **k: (0, []))
    assert release.cmd_merge(_merge_args(origin, evidence, cards)) == 1                 # no card yet
    (cards / "C900.md").write_text("ID: C900\n")
    assert release.cmd_merge(_merge_args(origin, evidence, cards)) == 0
    assert _git(origin, "log", "-1", "--format=%s").startswith("Merge C900 (a change)")


def test_merge_refuses_on_a_failing_test_run_whatever_its_last_line_says(mergeable, monkeypatch, capsys):
    origin, evidence, cards = mergeable
    (cards / "C900.md").write_text("ID: C900\n")
    monkeypatch.setattr(release, "run_checked", lambda *a, **k: (1, ["FAILED tests/test_a.py::test_x"]))
    before = _git(origin, "rev-parse", "HEAD")
    assert release.cmd_merge(_merge_args(origin, evidence, cards)) == 1
    assert "FAILED tests/test_a.py::test_x" in capsys.readouterr().out and _git(origin, "rev-parse", "HEAD") == before


def test_merge_refuses_while_two_errors_are_unreviewed(mergeable, monkeypatch):
    origin, evidence, cards = mergeable
    (cards / "C900.md").write_text("ID: C900\n")
    monkeypatch.setattr(release, "run_checked", lambda *a, **k: (0, []))
    log = evidence / "errors.jsonl"
    _errors(log, 2)
    assert release.cmd_merge(_merge_args(origin, evidence, cards, errors_file=log)) == 1


def test_a_checked_run_reads_the_exit_code_and_keeps_the_whole_output(tmp_path):
    code, failing = release.run_checked([sys.executable, "-c",
                                         "print('FAILED t.py::x'); print('1 failed'); print('all done')"
                                         "; raise SystemExit(1)"], tmp_path, tmp_path / "out.log")
    assert code == 1 and failing == ["FAILED t.py::x"] and "all done" in (tmp_path / "out.log").read_text()
