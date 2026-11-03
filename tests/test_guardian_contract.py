import json
from pathlib import Path

import pytest

from engine_core.guardian.contract import check_reply

MATRIX = json.loads((Path(__file__).resolve().parents[1] / "src" / "engine_core" / "guardian"
                     / "contract_matrix.json").read_text(encoding="utf-8"))["rows"]


@pytest.mark.parametrize("row", MATRIX, ids=[row["id"] for row in MATRIX])
def test_contract_check_agrees_with_the_matrix(row):
    assert (check_reply(row["exit"], row["reply"]) == []) is row["allowed"]


def test_the_exit_20_noop_without_already_satisfied_is_a_violation():
    reply = {"contract": 1, "status": "not_applicable", "error_code": "", "retryable": False}
    assert check_reply(20, reply) == ["exit_20_requires_not_applicable_and_ALREADY_SATISFIED"]


def test_ids_are_unique():
    assert len({row["id"] for row in MATRIX}) == len(MATRIX)


def test_limit_up_today_dry_run_reply_obeys_the_contract(tmp_path):
    from engine_core.guardian.limit_up_today import run
    code, reply = run({"trade_date": "2026-11-03", "args": {"tier": "redis"}}, tmp_path, dry_run=True)
    assert check_reply(code, reply) == []


def _run_main(monkeypatch, capsys, code, reply):
    import io
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from examples import guardian_handler
    monkeypatch.setattr(guardian_handler, "_run", lambda request: (code, reply))
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    exit_code = guardian_handler.main(["run"])
    captured = capsys.readouterr()
    return exit_code, json.loads(captured.out.strip().splitlines()[-1]), captured.err


def test_handler_flags_a_contract_violation_without_changing_the_exit_code(monkeypatch, capsys):
    reply = {"contract": 1, "status": "not_applicable", "error_code": "", "retryable": False}
    exit_code, printed, err = _run_main(monkeypatch, capsys, 20, reply)
    assert exit_code == 20
    assert printed["contract_violations"] == ["exit_20_requires_not_applicable_and_ALREADY_SATISFIED"]
    assert "CONTRACT_VIOLATION exit=20" in err


def test_handler_leaves_a_valid_reply_untouched(monkeypatch, capsys):
    reply = {"contract": 1, "status": "ok", "error_code": "", "retryable": False}
    exit_code, printed, err = _run_main(monkeypatch, capsys, 0, reply)
    assert (exit_code, printed, err) == (0, reply, "")
