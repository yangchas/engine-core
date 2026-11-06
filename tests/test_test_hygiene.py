from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from examples import guardian_selftest
from tests import test_guardian_e2e


def test_build_uses_shared_go_cache(tmp_path, monkeypatch):
    monkeypatch.delenv("GOCACHE", raising=False)
    with patch.object(guardian_selftest.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
        guardian_selftest._build_binary(tmp_path)
    env = run.call_args.kwargs["env"]
    assert Path(env["GOCACHE"]) == Path.home() / ".cache" / "go-build"
    assert not Path(env["GOCACHE"]).is_relative_to(tmp_path)
    monkeypatch.setenv("GOCACHE", "/tmp/custom-go-cache")
    with patch.object(guardian_selftest.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
        guardian_selftest._build_binary(tmp_path)
    assert run.call_args.kwargs["env"]["GOCACHE"] == "/tmp/custom-go-cache"


def test_e2e_work_dir_removed_on_exit():
    process = SimpleNamespace(communicate=lambda timeout: ("", ""), returncode=0)
    with patch.object(test_guardian_e2e.subprocess, "Popen", return_value=process) as popen:
        test_guardian_e2e.test_guardian_wall_clock_selftest_fast_suite()
    command = popen.call_args.args[0]
    assert not Path(command[command.index("--work-dir") + 1]).exists()
