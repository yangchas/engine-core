import csv
import json
import subprocess
import sys


def test_build_script_writes_versioned_outputs_and_rejects_nonempty_dir(tmp_path):
    old = tmp_path / "old.csv"
    old.write_text("股票代码,股票简称,总市值,a股市值(不含限售股)\n000001,甲,100,80\n", encoding="utf-8")
    groups = []
    for name, rows in (
        ("mkt_nost", [{"股票代码": "000001.SZ", "总市值[20261014]": "120", "a股市值(不含限售股)[20261014]": "90"},
                       {"股票代码": "000002.SZ", "总市值[20261014]": "--"}]),
        ("ind_st", [{"股票代码": "000002.SZ", "股票简称[20261014]": "乙", "所属同花顺行业[20261014]": "行业"}]),
    ):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "rows.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        (folder / "manifest.json").write_text(json.dumps({"complete": True}), encoding="utf-8")
        groups.append(folder)
    output = tmp_path / "out"
    repo = __import__("pathlib").Path(__file__).resolve().parents[1]
    script = repo / "examples" / "build_f10_refresh.py"
    cmd = [sys.executable, str(script), "--old-csv", str(old), "--wencai-dirs", ",".join(map(str, groups)),
           "--new-asof", "2026-10-14", "--output-dir", str(output)]
    subprocess.run(cmd, check=True, cwd=repo, capture_output=True, text=True)
    assert {p.name for p in output.iterdir()} == {
        "f10_refreshed.csv", "f10_provenance.json", "coverage_report.json", "manifest.json"}
    with (output / "f10_refreshed.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert {r["股票代码"] for r in rows} == {"000001", "000002"}
    assert all("_asof_mkt" in r and "_source_mkt" in r for r in rows)
    assert json.loads((output / "coverage_report.json").read_text(encoding="utf-8"))["row_count"] == 2
    (output / "keep").write_text("x")
    failed = subprocess.run(cmd, cwd=repo, capture_output=True, text=True)
    assert failed.returncode != 0
