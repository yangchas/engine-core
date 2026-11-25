import json

from engine_core.plate_static_relation import (
    build_static_relation,
    parse_plate_inner_edges,
    parse_stock_plate_csv,
)


def test_stock_plate_csv_zfills_deduplicates_and_counts_invalid_rows():
    counts = {}
    result = parse_stock_plate_csv(
        "plateid,stockid\n801001,2520\n801001,002520\n801002,12x\n801002,1234567\n",
        counts=counts,
    )
    assert result == {"801001": ("002520",)}
    assert counts == {"rows": 4, "discarded_rows": 2, "duplicate_pairs": 1}


def test_inner_edges_ignore_corrupt_names_and_count_bad_literal_rows():
    counts = {}
    edges = parse_plate_inner_edges(
        'id,name,x,inner\n'
        '801000,乱码,1,"[[\'801001\', \'����\', 1.0]]"\n'
        '801002,乱码,1,"not a literal"\n', counts=counts,
    )
    assert edges == [("801000", "801001")]
    assert counts == {"rows": 2, "invalid_literal_rows": 1}


def test_build_relation_keeps_child_members_only_and_exposes_parent_via_hierarchy():
    relation = build_static_relation(
        {"801001": ("000001",), "801000": ("000002",)},
        {"801001": "子板", "801000": "父板"}, [("801000", "801001")],
    )
    assert relation.by_symbol == {"000001": ("子板",), "000002": ("父板",)}
    assert relation.hierarchy.parent_by_child == {"子板": "父板"}
    assert relation.flags == ("STATIC_ASOF_UNKNOWN",)
    assert relation.asof is None


def test_unnamed_code_uses_code_label_and_unknown_kind():
    relation = build_static_relation({"801999": ("000001",)}, {}, [], unnamed_codes=["801999"])
    assert relation.plate_names == {"801999": "P801999"}
    assert relation.unnamed_codes == ("801999",)
    assert relation.by_symbol["000001"] == ("P801999",)


