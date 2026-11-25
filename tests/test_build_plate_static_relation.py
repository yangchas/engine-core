import json

from examples.build_plate_static_relation import build


def test_builder_emits_json_and_manifest(tmp_path):
    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    (csv_dir / "个股板块.csv").write_text("plateid,stockid\n801001,2520\n", encoding="utf-8")
    (csv_dir / "板块.csv").write_text(
        'id,name,x,inner\n801000,乱码,1,"[[\'801001\', \'子板\', 1]]"\n', encoding="utf-8"
    )
    dictionary = tmp_path / "dictionary.json"
    dictionary.write_text(json.dumps({"names": {"801000": "父板", "801001": "子板"},
                                      "edges": [["801000", "801001"]], "unnamed_codes": []}),
                          encoding="utf-8")
    output = tmp_path / "out"
    build(csv_dir, dictionary, output)
    payload = json.loads((output / "static_relation.json").read_text())
    manifest = json.loads((output / "manifest.json").read_text())
    assert payload["by_symbol"] == {"002520": ["子板"]}
    assert manifest["asof"] is None
    assert manifest["labels"] == ["STATIC_ASOF_UNKNOWN"]

