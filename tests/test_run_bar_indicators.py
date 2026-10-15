import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _ticks(path):
    path.write_text(
        "trade_date,ts,symbol,px_milli,pc_milli,bp1_milli,ap1_milli,bv1,av1,amt_yuan\n"
        "2026-10-14,09:24:00,600000,0,10000,10000,10000,10,10,0\n"
        "2026-10-14,09:25:00,600000,10100,10000,10100,10100,1,1,50500\n",
        encoding="utf-8",
    )


def test_cli_writes_json_markdown_html_and_hashed_manifest(tmp_path):
    ticks, output = tmp_path / "ticks.csv", tmp_path / "out"
    _ticks(ticks)
    result = subprocess.run([sys.executable, str(ROOT / "examples/run_bar_indicators.py"),
        "--trade-date", "2026-10-14", "--ticks-csv", str(ticks), "--out-dir", str(output)],
        cwd=ROOT, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert "bars=" in result.stdout and "turning_points=" in result.stdout
    data = json.loads((output / "bars.json").read_text(encoding="utf-8"))
    assert any(row["window_amount_yuan"] == 40500 for row in data["bars"])
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["input_sha256"]["ticks_csv"]
    assert (output / "bars_report.md").is_file() and "<svg" in (output / "bars_report.html").read_text(encoding="utf-8")


def test_cli_refuses_nonempty_output_dir(tmp_path):
    ticks, output = tmp_path / "ticks.csv", tmp_path / "out"
    _ticks(ticks)
    output.mkdir()
    (output / "keep.txt").write_text("keep", encoding="utf-8")
    result = subprocess.run([sys.executable, str(ROOT / "examples/run_bar_indicators.py"),
        "--trade-date", "2026-10-14", "--ticks-csv", str(ticks), "--out-dir", str(output)],
        cwd=ROOT, text=True, capture_output=True)
    assert result.returncode == 2
    assert "out-dir must be empty" in result.stderr
    assert (output / "keep.txt").read_text(encoding="utf-8") == "keep"
