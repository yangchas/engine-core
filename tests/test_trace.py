from io import StringIO

from engine_core.contracts import StrategyResult, canonical_json
from engine_core.trace import JsonTraceSink


class RecordingStream(StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.flush_count = 0

    def flush(self) -> None:
        self.flush_count += 1
        super().flush()


def test_json_trace_sink_emits_one_canonical_flushed_record() -> None:
    result = StrategyResult(
        strategy_id="probe",
        evaluation_id="evaluation-1",
        state="OBSERVED",
        trace={"z": 2, "a": {"value": 1}},
        evidence_refs=("fixture://auction/600519/0924",),
        content_hash="semantic-result-hash",
    )
    stream = RecordingStream()

    JsonTraceSink(stream).emit(result)

    assert stream.getvalue() == canonical_json(result) + "\n"
    assert stream.getvalue().count("\n") == 1
    assert stream.flush_count == 1


def test_json_trace_sink_resolves_default_stdout_at_construction(monkeypatch) -> None:
    redirected = RecordingStream()
    monkeypatch.setattr("engine_core.trace.sys.stdout", redirected)
    result = StrategyResult(
        strategy_id="probe",
        evaluation_id=None,
        state="MISSING",
        trace={},
        evidence_refs=(),
        content_hash="missing-result-hash",
    )

    JsonTraceSink().emit(result)

    assert redirected.getvalue() == canonical_json(result) + "\n"
    assert redirected.flush_count == 1
