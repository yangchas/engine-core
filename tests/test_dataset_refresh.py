from engine_core.dataset_refresh import DATASET_SCHEDULE, summarize_dataset_state
from engine_core.live_inputs import LiveInputState


def test_dataset_schedule_has_all_inputs_and_local_clock_times():
    expected = {
        "hot_plates", "hot_rank", "limit_truth", "yest_limit_pool",
        "stock_plate", "s2p", "anchor_archive",
    }
    assert {item["name"] for item in DATASET_SCHEDULE} == expected
    for item in DATASET_SCHEDULE:
        assert item["read_at"]
        assert all(
            len(value) == 8 and value[2] == ":" and value[5] == ":"
            for value in item["read_at"]
        )


def test_summarize_dataset_state_leaves_absent_metadata_unknown():
    state = LiveInputState("hot_plates", "MISSING", None, "empty_response", None, 10)
    summary = summarize_dataset_state(state, now_ms=20)
    assert summary == {
        "name": "hot_plates", "status": "MISSING", "row_count": None,
        "key": None, "last_refreshed_ms": None, "meta_phase": None,
        "meta_updated_at": None, "meta_updated_at_ts": None,
        "writer_age_ms": None, "reason": "empty_response",
    }
