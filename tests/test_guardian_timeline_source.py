"""timeline.json is the hand-edited source; schedule.json, its native testdata copy and the provenance sha follow it."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from engine_core.guardian.timeline import expand
from engine_core.guardian.timeline_check import validate_timeline

ROOT = Path(__file__).resolve().parents[1]
GUARDIAN = ROOT / "src" / "engine_core" / "guardian"


def test_shipped_timeline_is_valid():
    assert validate_timeline(json.loads((GUARDIAN / "timeline.json").read_text(encoding="utf-8"))) == []


def test_generated_files_are_in_sync_with_the_timeline():
    done = subprocess.run([sys.executable, str(ROOT / "examples" / "generate_schedule.py"), "--check"],
                          capture_output=True, text=True)
    assert done.returncode == 0, "run examples/generate_schedule.py after editing timeline.json: " + done.stdout


def test_native_copy_and_provenance_follow_the_schedule():
    source = (GUARDIAN / "schedule.json").read_bytes()
    assert (ROOT / "native/guardiand/testdata/schedule.json").read_bytes() == source
    provenance = json.loads((ROOT / "native/guardiand/testdata/provenance.json").read_text(encoding="utf-8"))
    assert provenance["sha256"] == hashlib.sha256(source).hexdigest()
    assert expand(json.loads((GUARDIAN / "timeline.json").read_text(encoding="utf-8"))) == json.loads(source)


def test_a_hand_edit_of_schedule_json_is_caught_and_regeneration_repairs_it(tmp_path):
    """The drift check must notice a schedule.json edited instead of timeline.json (run on a temporary copy)."""
    for relative in ("src/engine_core/guardian/timeline.json", "src/engine_core/guardian/schedule.json",
                     "native/guardiand/testdata/schedule.json", "native/guardiand/testdata/provenance.json"):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / relative).read_bytes())
    script = [sys.executable, str(ROOT / "examples" / "generate_schedule.py"), "--root", str(tmp_path)]
    assert subprocess.run([*script, "--check"], capture_output=True).returncode == 0
    schedule = tmp_path / "src/engine_core/guardian/schedule.json"
    schedule.write_text(schedule.read_text(encoding="utf-8").replace('"cost": "light"', '"cost": "heavy"', 1),
                        encoding="utf-8")
    caught = subprocess.run([*script, "--check"], capture_output=True, text=True)
    assert caught.returncode == 1 and "src/engine_core/guardian/schedule.json" in caught.stdout
    assert subprocess.run(script, capture_output=True).returncode == 0
    assert subprocess.run([*script, "--check"], capture_output=True).returncode == 0
    assert schedule.read_bytes() == (ROOT / "src/engine_core/guardian/schedule.json").read_bytes()
