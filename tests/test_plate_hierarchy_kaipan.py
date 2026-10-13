from engine_core.plate_hierarchy_kaipan import (
    parse_kaipan_pool,
    parse_kaipan_reason_hierarchy,
)


def test_parse_kaipan_pool_real_response_shape_and_metadata_page():
    response = {"info": [[["600693", "东百集团", 0, "", 1791768338, "并购重组",
                           320562016, 2691143344, 200624715, 267763843, -67139128,
                           302864557, "并购重组、零售", 4365872511, 6.94, 1,
                           None, None, None, None, None, None, None]],
                ["2026-10-12", "meta", "page"]], "errcode": "0"}
    parsed = parse_kaipan_pool([response])
    assert parsed["600693"].plate == "并购重组、零售"
    assert parsed["600693"].lb_days == 1


def test_parse_kaipan_pool_uses_max_lb_for_duplicate_symbol():
    def response(lb):
        row = ["000001", "平安银行"] + [None] * 10 + ["银行"] + [None, None, lb]
        return {"info": [[row], ["date", "metadata"]], "errcode": "0"}

    parsed = parse_kaipan_pool([response(2), response(5)])
    assert parsed["000001"].lb_days == 5
    empty_plate = ["000002", "样本"] + [None] * 13 + [1]
    parsed_empty = parse_kaipan_pool({"info": [[empty_plate], ["date", "metadata"]]})
    assert parsed_empty["000002"].plate == ""


def test_reason_hierarchy_records_codes_and_ignores_zero_parent():
    responses = [
        {"StockID": "000011", "List": [{"ZSCode": ["801827"],
         "Reason": "AI应用(AI视频)；据半年报", "PZSCode": "803023"}]},
        {"StockID": "000012", "List": [{"ZSCode": ["801828"],
         "Reason": "AI应用(AI视频)", "PZSCode": "803023"}]},
        {"StockID": "000014", "List": [{"ZSCode": ["801829"],
         "Reason": "智能制造(AI视频)", "PZSCode": "803024"}]},
        {"StockID": "000013", "List": [{"ZSCode": ["801001"],
         "Reason": "地产链(物业服务)", "PZSCode": "0"}]},
    ]
    hierarchy = parse_kaipan_reason_hierarchy(responses)
    assert hierarchy.parent_by_child["AI视频"] == "AI应用"
    assert hierarchy.source_by_child["AI视频"] == "kaipan_pzs"
    assert hierarchy.evidence_by_child["AI视频"] == {
        "zs_codes": ["801827", "801828"], "pzs_code": "803023",
        "symbols_count": 2, "conflict": True,
    }
    assert "物业服务" not in hierarchy.parent_by_child
