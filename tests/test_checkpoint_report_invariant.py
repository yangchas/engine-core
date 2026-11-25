from __future__ import annotations

import re

import pytest

from engine_core.live_session import (OPEN_CHECKPOINTS, assert_report_checkpoint_invariant,
                                      open_checkpoint_for_asof)
from engine_core.ladder_cluster import build_ladder_clusters


_CHECKPOINT_INDEX = {label: index for index, (label, _) in enumerate(OPEN_CHECKPOINTS)}
_P_FIELD = re.compile(r"^(?:p)?(09\d{2})(?:_|$)")


def _assert_report_not_future(report, asof):
    # Keep this scanner independent from the runtime walk and checkpoint selector.
    from datetime import datetime

    anchor = datetime.fromisoformat(asof)
    trade_date = str(report.get("trade_date") or anchor.date().isoformat())
    cutoffs = {"0925": "09:25:06", "0932": "09:32:00", "0935": "09:35:00",
               "0938": "09:38:00", "0940": "09:40:00", "0945": "09:45:30"}
    selected = max((label for label, cutoff in cutoffs.items() if cutoff <= anchor.strftime("%H:%M:%S")),
                   key=lambda label: list(cutoffs).index(label), default="0925")
    ceiling = _CHECKPOINT_INDEX[selected]

    def timestamp_checkpoint(value):
        try:
            observed = datetime.fromisoformat(value)
        except ValueError:
            observed = datetime.fromisoformat(f"{trade_date}T{value[-8:]}")
        if observed.date().isoformat() < trade_date:
            return "0925"
        assert observed.date().isoformat() == trade_date, "observation is not on trade_date"
        comparable = observed
        if comparable.tzinfo is not None and anchor.tzinfo is None:
            comparable = comparable.replace(tzinfo=None)
        elif anchor.tzinfo is not None and comparable.tzinfo is None:
            comparable = comparable.replace(tzinfo=anchor.tzinfo)
        assert comparable <= anchor, "observation timestamp exceeds report asof"
        value_clock = observed.strftime("%H:%M:%S")
        available = [label for label, cutoff in cutoffs.items() if cutoff <= value_clock]
        return available[-1] if available else "0925"

    def visit(value, path="report"):
        if isinstance(value, dict):
            for key, child in value.items():
                match = _P_FIELD.match(str(key))
                if match and match.group(1) in _CHECKPOINT_INDEX:
                    assert _CHECKPOINT_INDEX[match.group(1)] <= ceiling or child is None, path + "." + key
                if key in {"checkpoint", "slice_label", "current_checkpoint"} and isinstance(child, str):
                    if child in _CHECKPOINT_INDEX:
                        assert _CHECKPOINT_INDEX[child] <= ceiling, path + "." + key
                if key in {"promotion_checkpoint", "open_checkpoint"} and isinstance(child, str):
                    assert child in _CHECKPOINT_INDEX and _CHECKPOINT_INDEX[child] <= ceiling, path + "." + key
                if key in {"asof", "source_ts", "ts"} and isinstance(child, str):
                    # These are observation times, unlike stored-at/cache metadata.
                    try:
                        observed_checkpoint = timestamp_checkpoint(child)
                    except (TypeError, ValueError):
                        observed_checkpoint = None
                    assert observed_checkpoint is None or _CHECKPOINT_INDEX[observed_checkpoint] <= ceiling, path + "." + key
                if key in {"checkpoint", "slice_label", "current_checkpoint", "chapter",
                           "chapter_title", "title", "heading", "section_title", "count_label",
                           "emotion_asof", "label"} and isinstance(child, str):
                    for token in re.findall(r"(?<!\d)(09:\d{2}|09\d{2})(?!\d)", child):
                        label = token.replace(":", "")
                        if label in _CHECKPOINT_INDEX:
                            assert _CHECKPOINT_INDEX[label] <= ceiling, path + "." + key
                if key in {"sections", "chapter_titles"} and isinstance(child, list):
                    for item in child:
                        if isinstance(item, str):
                            for token in re.findall(r"(?<!\d)(09:\d{2}|09\d{2})(?!\d)", item):
                                label = token.replace(":", "")
                                if label in _CHECKPOINT_INDEX:
                                    assert _CHECKPOINT_INDEX[label] <= ceiling, path + "." + key
                visit(child, path + "." + str(key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}[{index}]")

    visit(report)


@pytest.mark.parametrize("payload", [
    {"ladder_section": {"promotion_checkpoint": "0945"}},
    {"stocks": [{"open_checkpoint": "0945"}]},
    {"chapters": {"market_price_volume": {"asof": "2026-10-16T09:45:30", "median_pct_change": 9}}},
    {"chapters": {"highboards": [{"source_ts": "2026-10-16T09:45:30", "price": 13}]}},
])
def test_runtime_invariant_rejects_future_in_actual_report_fields(payload):
    report = {"asof": "2026-10-16T09:32:10+08:00", "checkpoint": "0932", **payload}
    with pytest.raises(ValueError) as error:
        assert_report_checkpoint_invariant(report, "0932")
    assert error.value.code == "REPORT_CHECKPOINT_INVARIANT_VIOLATION"


@pytest.mark.parametrize("payload", [
    {"ladder_section": {"promotion_checkpoint": "0945"}},
    {"stocks": [{"open_checkpoint": "0945"}]},
    {"chapters": {"market_price_volume": {"asof": "2026-10-16T09:45:30", "median_pct_change": 9}}},
    {"chapters": {"highboards": [{"source_ts": "2026-10-16T09:45:30", "price": 13}]}},
])
def test_independent_scanner_rejects_future_in_actual_report_fields(payload):
    report = {"asof": "2026-10-16T09:32:10+08:00", "checkpoint": "0932", **payload}
    with pytest.raises(AssertionError):
        _assert_report_not_future(report, report["asof"])


@pytest.mark.parametrize(("asof", "checkpoint"), [
    ("2026-10-16T09:32:10+08:00", "0932"),
    ("2026-10-16T09:35:10+08:00", "0935"),
    ("2026-10-16T09:40:10+08:00", "0940"),
    ("2026-10-16T09:45:30+08:00", "0945"),
])
def test_report_checkpoint_labels_and_columns_never_exceed_asof(asof, checkpoint):
    report = {
        "asof": asof,
        "checkpoint": checkpoint,
        "chapters": {"highboards": {"chapter_title": f"{checkpoint}高标聚类"}},
        "stocks": [{"p0932_pct": 1.0,
                    "p0938_pct": 2.0 if checkpoint in {"0940", "0945"} else None,
                    "p0945_pct": None if checkpoint != "0945" else 3.0}],
    }
    _assert_report_not_future(report, asof)
    assert_report_checkpoint_invariant(report, checkpoint)


@pytest.mark.parametrize(("asof", "report"), [
    ("2026-10-16T09:32:10+08:00", {"checkpoint": "0938"}),
    ("2026-10-16T09:38:10+08:00", {"chapters": {"title": "0945高标"}}),
    ("2026-10-16T09:45:29+08:00", {"stocks": [{"p0945_pct": 1.0}]}),
    ("2026-10-16T09:32:10+08:00", {"streak_feedback": {"checkpoints": {"0935": {"rows": []}}}}),
    ("2026-10-16T09:38:10+08:00", {"emotion": {"label": "未到09:45截点"}}),
])
def test_report_rejects_future_checkpoint_data(asof, report):
    with pytest.raises(AssertionError):
        _assert_report_not_future(report, asof)
    checkpoint = open_checkpoint_for_asof(asof)
    with pytest.raises(ValueError):
        assert_report_checkpoint_invariant(report, checkpoint)


def test_report_asof_clock_is_not_a_future_checkpoint_label():
    report = {"asof": "2026-10-16T09:45:29"}
    assert_report_checkpoint_invariant(report, open_checkpoint_for_asof(report["asof"]))


@pytest.mark.parametrize(("asof", "checkpoint"), [
    ("2026-10-16T09:32:10+08:00", "0932"),
    ("2026-10-16T09:35:10+08:00", "0935"),
    ("2026-10-16T09:40:10+08:00", "0940"),
    ("2026-10-16T09:45:30+08:00", "0945"),
])
def test_scheduled_open_report_asof_selects_its_checkpoint(asof, checkpoint):
    assert open_checkpoint_for_asof(asof) == checkpoint


def test_report_observation_cannot_claim_storage_metadata_to_bypass_future_check():
    report = {"trade_date": "2026-10-16", "asof": "2026-10-16T09:32:10+08:00",
              "chapters": {"market_price_volume": {
                  "asof": "2026-10-16T09:45:30+08:00", "slice_id": "market_price_volume",
                  "source": "TD"}}}
    with pytest.raises(ValueError):
        assert_report_checkpoint_invariant(report, "0932")


@pytest.mark.parametrize("future", ["2026-10-16T09:32:11+08:00", "2026-10-17T09:32:00+08:00"])
def test_independent_scanner_rejects_full_timestamp_future(future):
    report = {"trade_date": "2026-10-16", "asof": "2026-10-16T09:32:10+08:00",
              "chapters": {"market_price_volume": {"source_ts": future}}}
    with pytest.raises(AssertionError):
        _assert_report_not_future(report, report["asof"])


@pytest.mark.parametrize("checkpoint", ["0932", "0938", "0945"])
def test_highboard_cluster_uses_selected_current_pct(checkpoint):
    product = build_ladder_clusters(
        [{"symbol": "600001", "name": "甲股", "streak": 3,
          "current_checkpoint": checkpoint, "current_pct": 5.0,
          checkpoint: 5.0, "0945": 5.0 if checkpoint == "0945" else None,
          "price": 10.5, "pre_close": 10.0, "match_amount_yuan": 1_000_000}],
        [], {}, stage="open",
        theme_assignments={"600001": {"theme_key": "p", "theme_name": "算力",
                                       "theme_source": "POOL_LABEL"}},
    )
    assert product["clusters"][0]["equal_weight_pct"] == 5.0
    assert "缺失[涨幅缺失]" not in product["clusters"][0]["leaders"][0]


def test_checkpoint_violation_hides_values_and_derived_report_sections():
    from engine_core.daily_pipeline import _degraded_checkpoint_report

    report = {
        "trade_date": "2026-10-16", "asof": "2026-10-16T09:32:00",
        "checkpoint": "0932", "chapters": {
            "market_price_volume": {"asof": "2026-10-16T09:45:30", "median_pct_change": 9.876543,
                                    "market_match_amount_yuan": 123456789},
            "ladder_clusters": {"count": 12, "equal_weight_pct": 8.7}},
        "stocks": [{"symbol": "600001", "price": 10.5, "p0945_pct": 9.876543}],
    }
    hidden = _degraded_checkpoint_report(report, "2026-10-16", "0932", "2026-10-16T09:32:00")
    encoded = __import__("json").dumps(hidden, ensure_ascii=False)
    assert "9.876543" not in encoded
    assert "123456789" not in encoded
    assert "10.5" not in encoded
    assert hidden["status"] == "DEGRADED"
    assert hidden["checkpoint"] == "0932"
