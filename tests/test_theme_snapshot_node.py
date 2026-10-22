import json
from pathlib import Path

from engine_core.data_pipeline.node_store import NodeStore
from engine_core.data_pipeline.storage import ArtifactCatalog
from engine_core.limit_reason_theme import load_kaipan_theme_snapshot


def test_theme_snapshot_node_reuses_only_the_full_versioned_pit_fingerprint(tmp_path):
    pool = [json.loads(line) for line in
            (Path(__file__).parent / "fixtures/real_limit_up_pool_sample.jsonl").read_text().splitlines()]
    inputs = {"yest_limit_pool": "pool-v1:2", "plate_registry": "registry-v1:1",
              "plate_members": "members-v1:0", "hot_plates_yesterday": "hot-v1:0",
              "limit_reason_history": "reasons-v1:0", "trade_date": "2026-10-22",
              "previous_trade_date": "2026-10-21", "calendar_dates_sha256": "calendar-a",
              "plate_names_sha256": "names-a"}

    def build():
        return load_kaipan_theme_snapshot(None, report_date="2026-10-22",
            previous_trade_date="2026-10-21", observed_at="2026-10-22T09:26:00+08:00",
            plate_names={}, pool_rows=pool, reason_rows=[], hot_rows=[], trading_dates=[])

    store = NodeStore(ArtifactCatalog(tmp_path))
    first, hit1 = store.compute("theme_snapshot", "2026-10-22", "ThemeSnapshotNodeV1:algorithm-v1",
                                inputs, build, asof="2026-10-22T08:50:00+08:00")
    second, hit2 = store.compute("theme_snapshot", "2026-10-22", "ThemeSnapshotNodeV1:algorithm-v1",
                                 inputs, lambda: {"wrong": True}, asof="2026-10-22T09:26:00+08:00")
    assert not hit1 and hit2 and second == first

    for key in ("yest_limit_pool", "plate_registry", "plate_members",
                "hot_plates_yesterday", "limit_reason_history"):
        version, count = inputs[key].rsplit(":", 1)
        for identity in (f"{version}-v2:{count}", f"{version}:{int(count) + 1}"):
            changed = {**inputs, key: identity}
            # A later PIT cutoff that admits one additional real source row
            # changes the count component and must create a separate version.
            _, hit = store.compute("theme_snapshot", "2026-10-22", "ThemeSnapshotNodeV1:algorithm-v1",
                                   changed, build)
            assert not hit
    _, hit = store.compute("theme_snapshot", "2026-10-22", "ThemeSnapshotNodeV1:algorithm-v2",
                           inputs, build)
    assert not hit
