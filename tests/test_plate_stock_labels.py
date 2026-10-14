from engine_core.plate_stock_labels import label_stock


def test_labels_dynamic_static_no_plate_and_flags():
    registry = {
        "801001": {"code": "801001", "name": "AI应用", "class": "THEME"},
        "801002": {"code": "801002", "name": "并购重组", "class": "EVENT"},
    }
    selected = [
        {"code": "801001", "name": "AI应用", "class": "THEME", "direction": "WEAK",
         "members": {"000001", "000002"}},
        {"code": "801003", "name": "通信", "class": "THEME", "direction": "STRONG",
         "members": {"000001"}},
        {"code": "801002", "name": "并购重组", "class": "EVENT", "direction": None,
         "members": {"000009"}},
    ]
    feature = type("Feature", (), {"primary": "801001", "flags": []})()
    members = {"801001": {"000001", "000002"}, "801003": {"000001"}}

    dynamic = label_stock("000001", selected, feature, registry, ret=0.10, is_limit_up=True)
    static = label_stock("000004", selected, feature, registry, ret=0.0, is_limit_up=False)
    no_plate = label_stock("000010", selected, None, registry, ret=None, is_limit_up=False)

    assert dynamic["source"] == "DYNAMIC"
    assert dynamic["plate"] == {"code": "801003", "name": "通信"}
    assert "COUNTER_TREND" in dynamic["flags"]
    assert static["source"] == "STATIC_NO_SIGNAL"
    assert no_plate["source"] == "NO_PLATE"
    assert "NO_PLATE" in no_plate["flags"]
