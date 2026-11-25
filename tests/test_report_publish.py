"""C642: the close report publishes through a writer it opens itself; failures name their exception first."""
import subprocess

import pytest

from engine_core.daily_pipeline import DailySources, _publish_report

DAY = "2026-11-02"


class Recorder:
    def __init__(self):
        self.sets = []

    def set(self, key, value, ex=None):
        self.sets.append((key, value, ex))
        return True


def test_the_close_report_opens_its_own_redis_writer_because_reading_never_connected_one():
    # before C642 this raised "--publish requires an isolated Redis client" after the report had been built
    writer, connected = Recorder(), []
    key = _publish_report(DailySources(), "close", DAY, '{"report": 1}', connect=lambda: connected.append(1) or writer)
    assert key == f"core:cache:report:{DAY}:close" and connected == [1]
    assert writer.sets == [(key, '{"report": 1}', 172800)]


def test_an_injected_redis_is_used_as_given_and_nothing_is_connected():
    writer = Recorder()
    key = _publish_report(DailySources(redis=writer), "close", DAY, "x", connect=lambda: pytest.fail("no connect"))
    assert writer.sets == [(key, "x", 172800)]


def test_other_stages_without_a_redis_still_refuse_to_publish():
    with pytest.raises(RuntimeError, match="requires an isolated Redis client"):
        _publish_report(DailySources(), "open", DAY, "x", connect=lambda: pytest.fail("not for open"))


def test_an_unreachable_redis_is_reported_by_name_not_as_a_generic_failure():
    def down():
        raise ConnectionError("refused")

    with pytest.raises(RuntimeError, match="report_publish_redis_unavailable:ConnectionError"):
        _publish_report(DailySources(), "close", DAY, "x", connect=down)


def test_the_published_key_is_inside_the_core_cache_prefix_the_redis_guard_allows():
    class AnyKey(Recorder):
        pass

    writer = AnyKey()
    assert _publish_report(DailySources(redis=writer), "close", DAY, "x").startswith("core:cache:")


def test_a_failed_command_names_its_exception_in_the_first_line_of_the_message():
    from examples.core_guardian import _command_failure

    traceback = ("Traceback (most recent call last):\n" + "  File \"x.py\", line 1, in main\n    call()\n" * 40 +
                 "RuntimeError: --publish requires an isolated Redis client\n")
    result = subprocess.CompletedProcess(["x"], 1, stdout="", stderr=traceback)
    message = str(_command_failure(result))
    head = message[:313]                                              # the ledger keeps about this much
    assert head.startswith("command_exit=1 last_stderr_line=RuntimeError: --publish requires an isolated Redis client")
    assert "stderr_tail:" in message                                  # the tail is still there after it
    assert str(_command_failure(subprocess.CompletedProcess(["x"], 3, stdout="", stderr=""))) == "command_exit=3"
