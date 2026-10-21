"""C371: every registry fetch_job argv placeholder must be one _run_fetch_job fills (KeyError 'artifact_root' broke hot_rank backfill)."""
import string
import sys

from engine_core.guardian import startup
from engine_core.guardian.registry import load_registry

SUPPORTED = {"python", "date", "trade_date", "item", "artifact_root"}


def test_registry_fetch_job_placeholders_are_supported():
    bad = {}
    for item in load_registry():
        spec = item.get("fetch_job")
        if not isinstance(spec, dict):
            continue
        names = {f for part in spec["argv"] for _, f, _, _ in string.Formatter().parse(part) if f}
        if names - SUPPORTED:
            bad[item["id"]] = sorted(names - SUPPORTED)
    assert not bad


def test_run_fetch_job_substitutes_artifact_root(tmp_path):
    script = tmp_path / "echo_root.py"
    script.write_text("import sys,json\nprint(json.dumps(dict(status='ok', root=sys.argv[1])))\n")
    item = {"id": "x", "fetch_job": {"argv": [sys.executable, str(script), "{artifact_root}"], "timeout_seconds": 20}}
    out = startup._run_fetch_job(item, "2026-10-21", 20, tmp_path)
    assert out.get("root") == str(tmp_path)
