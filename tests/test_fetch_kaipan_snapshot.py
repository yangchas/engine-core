import hashlib
import json

import pytest

from examples.fetch_kaipan_snapshot import fetch_all


class FakeAnalyzer:
    def __init__(self, pools=None, reasons=None, error=None):
        self.pools = pools or {}
        self.reasons = reasons or {}
        self.error = error
        self.calls = []

    def _call_api(self, method, **kwargs):
        self.calls.append((method, kwargs))
        if self.error:
            raise self.error
        return self.pools.get((kwargs["date"], kwargs["ban"]), [])

    def get_ban_reasons(self, symbol):
        self.calls.append(("reason", symbol))
        if self.error:
            raise self.error
        return self.reasons.get(symbol, [])


def test_fetch_writes_raw_pool_reason_and_manifest_hashes(tmp_path):
    pool = [{"ZSCode": "600001", "PZSCode": "600000", "Date": "2026-10-09", "extra": 7}]
    analyzer = FakeAnalyzer({("2026-10-09", "1"): pool}, {"600001": {"raw": ["原因"]}})
    out = tmp_path / "snapshot"
    manifest = fetch_all(analyzer, ["2026-10-09"], output_dir=out, max_ban=1,
                         sleep_s=0, sleep_fn=lambda _: None)
    assert json.loads((out / "pools/2026-10-09_ban1.json").read_text()) == pool
    assert json.loads((out / "reasons/600001.json").read_text()) == {"raw": ["原因"]}
    assert [call for call in analyzer.calls if call[0] == "reason"] == [("reason", "600001")]
    assert manifest["pool_counts"] == {"2026-10-09": {"1": 1}}
    assert manifest["request_count"] == 2
    for name, digest in manifest["sha256"].items():
        assert hashlib.sha256((out / name).read_bytes()).hexdigest() == digest
    assert set(path.relative_to(out).as_posix() for path in out.rglob("*") if path.is_file()) == {
        "pools/2026-10-09_ban1.json", "reasons/600001.json", "manifest.json"
    }


def test_max_requests_truncates_before_next_request(tmp_path):
    analyzer = FakeAnalyzer({("2026-10-09", "1"): [{"code": "600001"}]})
    manifest = fetch_all(analyzer, ["2026-10-09"], output_dir=tmp_path / "out",
                         max_ban=3, max_requests=1, sleep_s=0, sleep_fn=lambda _: None)
    assert manifest["request_count"] == 1
    assert manifest["truncated"] is True
    assert len(analyzer.calls) == 1


def test_eight_consecutive_empty_responses_abort(tmp_path):
    analyzer = FakeAnalyzer()
    manifest = fetch_all(analyzer, ["2026-10-09"], output_dir=tmp_path / "out",
                         max_ban=12, sleep_s=0, sleep_fn=lambda _: None)
    assert manifest["request_count"] == 8
    assert manifest["aborted"] is True
    assert len(manifest["failures"]) == 8


def test_nonempty_output_directory_is_rejected(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    (out / "existing").write_text("keep")
    with pytest.raises(FileExistsError):
        fetch_all(FakeAnalyzer(), ["2026-10-09"], output_dir=out,
                  sleep_s=0, sleep_fn=lambda _: None)
    assert (out / "existing").read_text() == "keep"


def test_exception_messages_and_sensitive_terms_are_not_serialized(tmp_path):
    analyzer = FakeAnalyzer(error=RuntimeError("Token=secret DeviceID=private"))
    out = tmp_path / "out"
    manifest = fetch_all(analyzer, ["2026-10-09"], output_dir=out, max_ban=1,
                         sleep_s=0, sleep_fn=lambda _: None)
    serialized = (out / "manifest.json").read_text()
    assert manifest["failures"] == [{"error_type": "RuntimeError", "date": "2026-10-09", "lvl": 1}]
    assert "secret" not in serialized and "private" not in serialized
    assert "Token" not in serialized and "DeviceID" not in serialized
