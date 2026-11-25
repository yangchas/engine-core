import json
import multiprocessing as mp
from pathlib import Path

from engine_core.data_pipeline.storage import ArtifactCatalog

FIXTURE = Path(__file__).parent / "fixtures" / "real_limit_up_pool_sample.jsonl"


def _commit(args):
    root, idx = args
    rows = [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines()]
    rows = [dict(row, _writer=idx) for row in rows]
    ArtifactCatalog(root).commit(f"lock_probe_{idx % 4}", "2026-10-21", rows, {"as_of": "2026-10-21"}, source="t")


def test_concurrent_commits_never_lose_catalog_entries(tmp_path):
    n = 24
    with mp.get_context("fork").Pool(8) as pool:
        pool.map(_commit, [(str(tmp_path), i) for i in range(n)])
    datasets = ArtifactCatalog(tmp_path).read()["datasets"]
    versions = sum(len(entry["dates"]["2026-10-21"]) for entry in datasets.values())
    assert versions == n
