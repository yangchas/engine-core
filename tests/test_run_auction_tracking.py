import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _input(path: Path):
    path.write_text(
        "trade_date,ts,symbol,px_milli,pc_milli,bp1_milli,ap1_milli,bv1,av1,bp2_milli,ap2_milli,bv2,av2,amt_yuan,vol_units\n"
        "2026-10-14,09:15:01,600000,0,10000,10000,10000,10,10,0,0,0,0,0,0\n",
        encoding="utf-8",
    )


def test_cli_writes_timeline_events_report_and_manifest(tmp_path):
    ticks, output = tmp_path / "ticks.csv", tmp_path / "out"
    _input(ticks)
    result = subprocess.run([
        sys.executable, str(ROOT / "examples/run_auction_tracking.py"),
        "--trade-date", "2026-10-14", "--ticks-csv", str(ticks), "--out-dir", str(output),
    ], cwd=ROOT, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert "slices=21" in result.stdout
    timeline = json.loads((output / "timeline.json").read_text(encoding="utf-8"))
    assert timeline["timeline"][1]["market_match_amount_yuan"] == 10000
    assert (output / "events.json").is_file()
    assert (output / "tracking_report.md").is_file()
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["inputs"]["ticks_csv_sha256"]


def test_cli_refuses_nonempty_output_directory(tmp_path):
    ticks, output = tmp_path / "ticks.csv", tmp_path / "out"
    _input(ticks)
    output.mkdir()
    (output / "keep.txt").write_text("keep", encoding="utf-8")
    result = subprocess.run([
        sys.executable, str(ROOT / "examples/run_auction_tracking.py"),
        "--trade-date", "2026-10-14", "--ticks-csv", str(ticks), "--out-dir", str(output),
    ], cwd=ROOT, text=True, capture_output=True)
    assert result.returncode == 2
    assert "out-dir must be empty" in result.stderr
    assert (output / "keep.txt").read_text(encoding="utf-8") == "keep"
