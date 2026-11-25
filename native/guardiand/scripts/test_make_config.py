import json
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).with_name("make_config.py")


def test_make_config_injects_only_deployment_fields(tmp_path):
    src, dst = tmp_path / "source.json", tmp_path / "out.json"
    config = {
        "schema_version": 1, "config_version": 4, "timezone": "Asia/Shanghai",
        "heavy_max_load1": 2.5, "heavy_cutoffs": {"PRE_MARKET": "09:10:00"},
        "handlers": {"legacy_known": {"supports_degrade": True}},
        "events": [{"id": "sample", "handler": "legacy_known", "late_until": "10:00:00",
                    "cost": "heavy", "retry": {"max_attempts": 4, "backoff": "15s"}}],
    }
    src.write_text(json.dumps(config))

    subprocess.run([sys.executable, str(SCRIPT), str(src), str(dst), str(tmp_path), sys.executable],
                   check=True, capture_output=True, text=True)
    generated = json.loads(dst.read_text())

    assert generated["heavy_max_load1"] == 2.5
    assert generated["heavy_cutoffs"] == {"PRE_MARKET": "09:10:00"}
    assert generated["events"] == config["events"]
    assert generated["handlers"]["legacy_known"]["cwd"] == str(tmp_path)
    assert generated["handlers"]["legacy_known"]["argv"][-2:] == ["--handler", "known"]
    assert generated["handlers"]["alert"]["argv"][-1] == "--dry-run"
