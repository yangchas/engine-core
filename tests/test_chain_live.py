"""P1/L4 live contract (read-only): the sweep's plan on the REAL ~/core-data satisfies the invariants, whatever the values."""
from pathlib import Path

import pytest

from engine_core.data_pipeline.chain import FRESH, MISSING, STALE, UNDECIDABLE, chain_days, load_chain_config, make_status_fn, plan, read_manifest_file
from engine_core.data_pipeline.storage import ArtifactCatalog

ROOT = Path("~/core-data").expanduser()


@pytest.mark.live
def test_every_chain_node_has_a_known_status_and_the_plan_respects_dependency_order():
    if not (ROOT / "catalog.json").is_file():
        pytest.skip("no live core-data")
    config = load_chain_config("daily_kline")
    snapshot = ArtifactCatalog(ROOT).read()
    status = make_status_fn(read_manifest_file)
    days = chain_days(snapshot, config, "9999-12-31")
    assert 1 <= len(days) <= config.window_days
    for dataset in config.nodes:
        for day in days:
            assert status(snapshot, dataset, day)[0] in {FRESH, STALE, MISSING, UNDECIDABLE}
    items, blocked = plan(snapshot, config, days, status)
    position = {(item.dataset, item.day): index for index, item in enumerate(items)}
    for (dataset, day), index in position.items():
        for need in config.nodes[dataset]:
            if (need, day) in position:
                assert position[(need, day)] < index        # a requirement is always built before its dependent
    assert all(item.day in days for item in items)
