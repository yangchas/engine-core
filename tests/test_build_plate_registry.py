import builtins
import json

import pytest

from examples.build_plate_registry import build


def test_builder_writes_registry_relations_and_missing_numpy_manifest(tmp_path, monkeypatch):
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    (csv_dir / "个股板块.csv").write_text(
        "plateid,stockid\n801001,2520\n801001,1\n", encoding="utf-8"
    )
    (csv_dir / "板块.csv").write_text(
        'id,name,x,inner\n801000,父板,1,"[[\'801001\', \'子板\', 1]]"\n',
        encoding="utf-8",
    )
    dictionary = tmp_path / "dictionary.json"
    dictionary.write_text(json.dumps({
        "names": {"801000": "父板", "801001": "子板"},
        "edges": [["801000", "801001"]], "unnamed_codes": [],
    }), encoding="utf-8")
    original_import = builtins.__import__

    def no_numpy(name, *args, **kwargs):
        if name == "numpy":
            raise ModuleNotFoundError("numpy unavailable", name="numpy")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_numpy)
    output = tmp_path / "out"
    build(csv_dir, dictionary, output, as_of="2026-10-13")
    registry = json.loads((output / "registry.json").read_text())
    manifest = json.loads((output / "manifest.json").read_text())
    assert registry["plates"][0]["code"] == "801000"
    assert (output / "relation.csv").read_text().splitlines() == [
        "plateid,stockid", "801001,000001", "801001,002520",
    ]
    assert manifest["as_of"] == "2026-10-13"
    assert manifest["npz"] == "skipped"
    assert not (output / "relation.npz").exists()


def test_builder_rejects_nonempty_output_directory(tmp_path):
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    dictionary = tmp_path / "dictionary.json"
    dictionary.write_text("{}", encoding="utf-8")
    output = tmp_path / "out"
    output.mkdir()
    (output / "keep").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="must be empty"):
        build(csv_dir, dictionary, output, as_of="2026-10-13")
