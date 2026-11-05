# DATA: real:kaipan_hot_plates_2026-10-23_1441.json, kaipan_pool_2026-10-23_L1_1444.json (replayed raw Kaipan responses);
# spec:evaluator inputs are ledger/reply shapes declared inline (the evaluator's own logic is what is under test)
"""C665: the whole-day replay harness - replay answers, gap reporting, and the PASS/FAIL verdict."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples"))
import replay_day  # noqa: E402
import replay_handler  # noqa: E402


@pytest.fixture
def replay_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(replay_handler, "REPLAY_DIR", tmp_path / "replay")
    return tmp_path / "replay"


def test_recorded_kaipan_requests_are_answered_from_the_recording(replay_dir):
    sdk = replay_handler.ReplayKaipan()
    plates = sdk.getHisPlates(date="2026-10-23", index="0", size="50")
    assert plates["Day"] == ["2026-10-23"] and plates["list"]
    pool = sdk.getHisBans(date="2026-10-23", ban="1", size="2000")
    assert isinstance(pool, dict)
    assert not (replay_dir / "unrecorded.jsonl").exists()


def test_unrecorded_requests_fail_and_are_listed_not_hidden(replay_dir):
    sdk = replay_handler.ReplayKaipan()
    with pytest.raises(replay_handler.UnrecordedRequest, match="UNRECORDED_REQUEST"):
        sdk.getHisPlates(date="2026-01-05", index="0", size="50")      # a day with no recording
    with pytest.raises(replay_handler.UnrecordedRequest):
        sdk.getHisBans(date="2026-10-23", ban="5", size="2000")        # a recorded day, an unrecorded level
    rows = [json.loads(line) for line in (replay_dir / "unrecorded.jsonl").read_text().splitlines()]
    assert [(r["source"], r["call"]) for r in rows] == [("kaipan", "getHisPlates"), ("kaipan", "getHisBans")]


def test_baostock_and_wencai_stand_ins_refuse_instead_of_calling_out(replay_dir):
    stand_in = replay_handler._Unreplayed("baostock", "baostock")        # used directly: install() patches the process
    with pytest.raises(replay_handler.UnrecordedRequest):
        stand_in.login()
    assert json.loads((replay_dir / "unrecorded.jsonl").read_text().splitlines()[0])["source"] == "baostock"


def test_virtual_time_only_moves_when_something_sleeps():
    virtual = replay_handler.VirtualTime()
    start_time, start_monotonic = virtual.time(), virtual.monotonic()
    started = time.monotonic()
    virtual.sleep(240)
    assert time.monotonic() - started < 5                                # no real waiting
    assert virtual.time() - start_time >= 240 and virtual.monotonic() - start_monotonic >= 240


def _run(tmp_path, *, states, replies, unrecorded=(), errors=()):
    state_root, replay = tmp_path / "state-root", tmp_path / "replay"
    (state_root / "ledger").mkdir(parents=True)
    replay.mkdir()
    rows = [{"type": "INSTANCE_STATE", "data": {"instance": name, "state": state}} for name, state in states.items()]
    (state_root / "ledger" / "2026-10-23.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (replay / "replies.jsonl").write_text("".join(
        json.dumps({"command": "run", "instance": i, "reply": reply}) + "\n" for i, reply in replies))
    (replay / "unrecorded.jsonl").write_text("".join(json.dumps(u) + "\n" for u in unrecorded))
    return {"day": "2026-10-23", "state_root": state_root, "replay_dir": replay, "errors": list(errors),
            "steps": 5, "expected": sorted(states), "wall_seconds": 1.0}


OK = {"contract": 1, "status": "ok", "error_code": "", "retryable": False}
FAILED_REPLY = {"contract": 1, "status": "failed", "error_code": "DETERMINISTIC_FAILURE", "retryable": False}


def test_verdict_passes_and_attributes_a_failure_to_the_gap_its_attempts_hit(tmp_path):
    run = _run(tmp_path,
               states={"a": {"status": "SUCCEEDED"}, "b": {"status": "FAILED_FINAL"}},
               replies=[("a", OK), ("b", FAILED_REPLY)],
               unrecorded=[{"instance": "b", "source": "kaipan", "call": "getHisBans"}])
    report = replay_day.evaluate(run)
    assert report["verdict"] == "PASS" and report["data_verdict"] == "PASS", report
    assert report["failures_attributed_to_gap"] == {"kaipan": 1}
    assert report["recorded_gaps"] == {"kaipan.getHisBans": 1}


def test_a_failure_without_a_gap_is_listed_and_fails_only_under_strict_data(tmp_path):
    run = _run(tmp_path, states={"a": {"status": "FAILED_FINAL", "last_error": "boom"}}, replies=[("a", FAILED_REPLY)])
    report = replay_day.evaluate(run)
    assert report["verdict"] == "PASS" and report["data_verdict"] == "UNATTRIBUTED_FAILURES"
    assert [u["instance"] for u in report["unattributed_failures"]] == ["a"]
    strict = replay_day.evaluate(run, strict_data=True)
    assert strict["verdict"] == "FAIL" and any("UNATTRIBUTED_FAILURE a" in p for p in strict["problems"])


@pytest.mark.parametrize("what,states,replies,errors,fragment", [
    ("a stuck instance", {"a": {"status": "RUNNING"}}, [("a", OK)], [], "NOT_TERMINAL a"),
    ("a contract violation", {"a": {"status": "SUCCEEDED"}},
     [("a", {**OK, "contract_violations": ["exit 20 without ALREADY_SATISFIED"]})], [], "CONTRACT_VIOLATION a"),
    ("an internal error", {"a": {"status": "SUCCEEDED_DEGRADED"}},
     [("a", {"contract": 1, "status": "failed", "error_code": "INTERNAL_ERROR", "retryable": True})], [],
     "UNMAPPED_ERROR a"),
    ("a daemon that stopped settling", {"a": {"status": "SUCCEEDED"}}, [("a", OK)],
     ["daemon did not settle after a clock step"], "did not settle"),
])
def test_verdict_fails_on_each_kind_of_orchestration_defect(tmp_path, what, states, replies, errors, fragment):
    report = replay_day.evaluate(_run(tmp_path, states=states, replies=replies, errors=errors))
    assert report["verdict"] == "FAIL", what
    assert any(fragment in problem for problem in report["problems"]), report["problems"]


def test_deferred_dependents_of_a_failed_upstream_are_reported_not_called_stuck(tmp_path):
    run = _run(tmp_path, states={
        "a": {"status": "SUCCEEDED"},
        "fetch_item:root": {"status": "FAILED_FINAL"},
        "fetch_item:child": {"status": "DEFERRED", "wait_for": ["fetch_item:root"]},
        "fetch_item:orphan": {"status": "DEFERRED", "wait_for": ["fetch_item:nothing"]}},
        replies=[("a", OK)])
    run["expected"] = ["a"]
    report = replay_day.evaluate(run)
    assert report["blocked_dependents"] == {"fetch_item:child": "fetch_item:root"}
    assert any("NOT_TERMINAL fetch_item:orphan" in p for p in report["problems"])     # no failed root: genuinely stuck


def test_redis_and_td_stand_ins_record_gaps_and_fail_like_unreachable_servers(replay_dir):
    with pytest.raises(ConnectionError, match="UNRECORDED_REQUEST:redis.smembers"):
        replay_handler._GapRedis.from_url("redis://replay.invalid:6379/0").smembers("q2:active:20261023")
    with pytest.raises(replay_handler.UnrecordedRequest, match="UNRECORDED_REQUEST:td.connect"):
        replay_handler._gap_taos_connect(database="market_data1")
    rows = [json.loads(line) for line in (replay_dir / "unrecorded.jsonl").read_text().splitlines()]
    assert [(r["source"], r["call"]) for r in rows] == [("redis", "smembers"), ("td", "connect")]


def test_child_processes_of_a_handler_are_replayed_too_and_run_on_virtual_time(tmp_path):
    """The handlers start real job scripts as child processes; those must not reach the real sources either."""
    code = ("import time, baostock\n"
            "start = time.monotonic()\n"
            "time.sleep(240)\n"                                    # virtual: returns at once
            "assert time.monotonic() - start >= 240\n"
            "try:\n"
            "    baostock.login()\n"
            "except Exception as exc:\n"
            "    print(type(exc).__name__)\n")
    env = {**os.environ, "CORE_REPLAY_DIR": str(tmp_path / "replay"), "CORE_REPLAY_INSTANCE": "inst_x",
           "PYTHONPATH": os.pathsep.join([str(ROOT / "examples" / "replay_site"), str(ROOT / "src")])}
    started = time.monotonic()
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)
    assert result.returncode == 0 and result.stdout.strip() == "UnrecordedRequest", result
    assert time.monotonic() - started < 20                           # not 240 real seconds
    gap = json.loads((tmp_path / "replay" / "unrecorded.jsonl").read_text().splitlines()[0])
    assert (gap["source"], gap["instance"]) == ("baostock", "inst_x")


def test_any_internet_connection_in_a_replayed_process_is_a_recorded_gap(tmp_path):
    code = ("import socket, urllib.request\n"
            "for attempt in (lambda: socket.create_connection(('127.0.0.1', 6379), 2),\n"
            "                lambda: urllib.request.urlopen('http://quote.example.invalid/x', timeout=2)):\n"
            "    try:\n"
            "        attempt()\n"
            "    except OSError as exc:\n"
            "        print('blocked')\n"
            "    else:\n"
            "        print('REACHED')\n"
            "a, b = socket.socketpair()\n"                       # local IPC must keep working
            "a.sendall(b'x'); print(b.recv(1))\n")
    env = {**os.environ, "CORE_REPLAY_DIR": str(tmp_path / "replay"), "CORE_REPLAY_INSTANCE": "inst_n",
           "PYTHONPATH": os.pathsep.join([str(ROOT / "examples" / "replay_site"), str(ROOT / "src")])}
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=60)
    assert result.stdout.split() == ["blocked", "blocked", "b'x'"], result
    sources = {json.loads(line)["source"] for line in (tmp_path / "replay" / "unrecorded.jsonl").read_text().splitlines()}
    assert sources == {"network"}


def test_a_child_that_replaces_pythonpath_is_still_sealed_through_the_interpreter_wrapper(tmp_path):
    """prep_trade_day._run() starts job scripts with PYTHONPATH=repo/src:repo, which drops the site hook."""
    script = tmp_path / "job.py"
    script.write_text("import socket\ntry:\n    socket.create_connection(('127.0.0.1', 6379), 2)\n"
                      "except OSError:\n    print('sealed')\nelse:\n    print('REACHED')\n")
    env = {**os.environ, "CORE_REPLAY_DIR": str(tmp_path / "replay"), "CORE_REPLAY_INSTANCE": "inst_w",
           "CORE_REPLAY_REAL_PYTHON": sys.executable, "PYTHONPATH": str(ROOT / "src")}      # no replay_site on it
    wrapper = ROOT / "examples" / "replay_python"
    result = subprocess.run([str(wrapper), str(script)], capture_output=True, text=True, env=env, timeout=60)
    assert result.stdout.strip() == "sealed", result
    inline = subprocess.run([str(wrapper), "-c", "import baostock\ntry:\n    baostock.login()\nexcept Exception as e:\n"
                             "    print(type(e).__name__)"], capture_output=True, text=True, env=env, timeout=60)
    assert inline.stdout.strip() == "UnrecordedRequest", inline


def test_grandchildren_are_sealed_too(tmp_path):
    """A job started by a job (prep_trade_day -> run_data_jobs -> ...) goes through the wrapper as well."""
    inner = tmp_path / "inner.py"
    inner.write_text("import socket\ntry:\n    socket.create_connection(('127.0.0.1', 6379), 2)\n"
                     "except OSError:\n    print('sealed')\nelse:\n    print('REACHED')\n")
    outer = tmp_path / "outer.py"
    outer.write_text("import subprocess, sys, os\nenv = dict(os.environ, PYTHONPATH='/nonexistent')\n"
                     f"print(subprocess.run([sys.executable, {str(inner)!r}], env=env, capture_output=True, text=True).stdout.strip())\n")
    env = {**os.environ, "CORE_REPLAY_DIR": str(tmp_path / "replay"), "CORE_REPLAY_REAL_PYTHON": sys.executable,
           "PYTHONPATH": str(ROOT / "src")}
    result = subprocess.run([str(ROOT / "examples" / "replay_python"), str(outer)], capture_output=True, text=True,
                            env=env, timeout=60)
    assert result.stdout.strip() == "sealed", result


def test_a_day_without_a_working_handler_cannot_pass(tmp_path):
    """FAIL_FIRST: with no usable handler the day cannot come out PASS."""
    broken = tmp_path / "no_such_handler.py"
    result = subprocess.run(
        [sys.executable, str(ROOT / "examples" / "replay_day.py"), "--date", "2026-10-23", "--handler", str(broken),
         "--work-dir", str(tmp_path / "work"), "--wall-limit", "60"],
        cwd=ROOT, capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    assert result.returncode != 0


# ---- end to end: a real guardiand, a real slice of the recorded day, and a deliberately broken handler ----------------
needs_go = pytest.mark.skipif(not Path("/bin/go").is_file(), reason="needs the Go toolchain to build guardiand")

MUTANT = """
import sys
sys.path.insert(0, {examples!r}); sys.path.insert(0, {src!r}); sys.path.insert(0, {root!r})
import guardian_handler
import replay_handler
real_run = guardian_handler._run

def broken(request):
    code, reply = real_run(request)
    if request.get("event") == "calendar_refresh_0005":
        # exit 20 ("already satisfied") without the ALREADY_SATISFIED code: forbidden by the contract matrix (C648)
        return 20, {{"contract": 1, "status": "not_applicable", "error_code": "", "retryable": False, "artifacts": []}}
    return code, reply

guardian_handler._run = broken
raise SystemExit(replay_handler.main())
"""

UNREGISTERED_MUTANT = MUTANT.replace(
    '''return 20, {{"contract": 1, "status": "not_applicable", "error_code": "", "retryable": False, "artifacts": []}}''',
    '''return 40, {{"contract": 1, "status": "failed", "error_code": "SOME_NEW_CODE_NOBODY_REGISTERED", "retryable": False, "artifacts": []}}''')


def _replay_slice(tmp_path, handler=None):
    command = [sys.executable, str(ROOT / "examples" / "replay_day.py"), "--date", "2026-10-23", "--from", "00:00:00",
               "--until", "00:30:00", "--wall-limit", "600", "--work-dir", str(tmp_path / "work"),
               "--report", str(tmp_path / "report.json")]
    if handler:
        command += ["--handler", str(handler)]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=900,
                            env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
    return result.returncode, json.loads((tmp_path / "report.json").read_text())


@needs_go
def test_a_clean_slice_of_the_recorded_day_passes_through_a_real_guardiand(tmp_path):
    code, report = _replay_slice(tmp_path)
    assert code == 0 and report["verdict"] == "PASS", report["problems"]
    assert report["instances"] >= 1 and report["replies"] >= 1


@needs_go
def test_a_handler_that_breaks_the_contract_makes_the_replay_fail(tmp_path):
    """Mutation check: the same slice with one handler answering outside the contract matrix must FAIL."""
    mutant = tmp_path / "mutant_handler.py"
    mutant.write_text(MUTANT.format(examples=str(ROOT / "examples"), src=str(ROOT / "src"), root=str(ROOT)))
    code, report = _replay_slice(tmp_path, handler=mutant)
    assert code == 1 and report["verdict"] == "FAIL"
    assert any("CONTRACT_VIOLATION calendar_refresh_0005" in p for p in report["problems"]), report["problems"]


@needs_go
def test_a_handler_answering_with_an_unregistered_code_makes_the_replay_fail(tmp_path):
    """Mutation check (design/33 S5): the same slice, one handler answering a code nobody registered, must FAIL."""
    mutant = tmp_path / "unregistered_handler.py"
    mutant.write_text(UNREGISTERED_MUTANT.format(examples=str(ROOT / "examples"), src=str(ROOT / "src"), root=str(ROOT)))
    code, report = _replay_slice(tmp_path, handler=mutant)
    assert code == 1 and report["verdict"] == "FAIL"
    assert any("UNREGISTERED_CODE calendar_refresh_0005: SOME_NEW_CODE_NOBODY_REGISTERED" in p
               for p in report["problems"]), report["problems"]


def test_a_reply_with_an_unregistered_error_code_is_an_orchestration_defect(tmp_path):
    run = _run(tmp_path, states={"a": {"status": "FAILED_FINAL"}},
               replies=[("a", {"contract": 1, "status": "failed", "error_code": "SOME_NEW_CODE_NOBODY_REGISTERED",
                               "retryable": False})],
               unrecorded=[{"instance": "a", "source": "kaipan", "call": "getHisBans"}])
    report = replay_day.evaluate(run)
    assert report["verdict"] == "FAIL"
    assert any("UNREGISTERED_CODE a: SOME_NEW_CODE_NOBODY_REGISTERED" in p for p in report["problems"])


def test_unclassified_replies_are_listed_and_fail_only_under_strict_codes(tmp_path):
    reply = {"contract": 1, "status": "failed", "error_code": "INTERNAL_ERROR", "retryable": True, "unclassified": True,
             "detail": "RuntimeError:boom"}
    run = _run(tmp_path, states={"a": {"status": "SUCCEEDED_DEGRADED"}}, replies=[("a", reply)])
    lenient = replay_day.evaluate(run)
    assert [u["instance"] for u in lenient["unclassified_replies"]] == ["a"]
    assert not any(p.startswith("UNCLASSIFIED") for p in lenient["problems"])
    strict = replay_day.evaluate(run, strict_codes=True)
    assert any(p.startswith("UNCLASSIFIED a:") for p in strict["problems"])
