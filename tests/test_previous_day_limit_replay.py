import json
import os

import pytest

from engine_core.previous_day_limit_replay import previous_day_limit_pool_from_artifact

POOL = "/home/exedev/validation/limit-pool-input-20261013/pool_2026-10-13_prev_2026-10-12.json"


@pytest.mark.skipif(not os.path.exists(POOL), reason="sealed pool artifact not present")
def test_sealed_pool_is_partial_with_rows():
    artifact = json.load(open(POOL))
    result = previous_day_limit_pool_from_artifact(
        artifact, trade_date="2026-10-13", cutoff_ms=1_760_000_000_000
    )
    assert result.status.value == "PARTIAL"
    assert len(result.data["rows"]) == len(artifact["data"]["rows"])
