import json
import subprocess
import sys
from pathlib import Path


def test_cli_dry_run_reads_fixture_without_redis_connection(tmp_path):
    root = Path(__file__).resolve().parents[1]
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({
        "observed_at": "2026-10-15T10:00:00+08:00",
        "q2": {"000001": {"px": "10100", "pc": "10000", "amt": "500", "ts": "1792029600000", "ls": "0"}},
        "relations": {}, "baseline": {}, "high_limit_pool": {},
    }), encoding="utf-8")
    result = subprocess.run([
        sys.executable, str(root / "examples/run_q2_tracker.py"),
        "--trade-date", "2026-10-15", "--input-json", str(fixture), "--dry-run",
    ], cwd=root, text=True, capture_output=True, check=True, env={**__import__("os").environ,
                                                                       "REDIS_HOST": "invalid-host-for-dry-run"})
    assert result.stderr == ""
    row = json.loads(result.stdout)
    assert row["dry_run"] is True
    assert row["market"]["fresh_count"] == 1
