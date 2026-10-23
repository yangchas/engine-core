import json
import sys
from pathlib import Path
from time import strftime as real_strftime
from types import SimpleNamespace

from engine_core.data_pipeline.storage import ArtifactCatalog
from engine_core.data_pipeline.providers import kaipan_provider
from examples import capture_kaipan_plates


ROOT = Path(__file__).resolve().parents[1]


def test_hot_plates_capture_commits_a_new_fixture_version_each_run(tmp_path, monkeypatch, capsys):
    response = json.loads((ROOT / "tests/fixtures/real/kaipan_hot_plates_2026-10-23_1441.json")
                          .read_text(encoding="utf-8"))

    class FixtureSdk:
        def getHisPlates(self, *_args):
            return response

    class MemoryRedis:
        def __init__(self):
            self.values = {}

        def get(self, key):
            return self.values.get(key)

        def set(self, key, value, ex=None):
            self.values[key] = value

        def delete(self, key):
            self.values.pop(key, None)

        def close(self):
            pass

    client = MemoryRedis()
    monkeypatch.setattr(capture_kaipan_plates, "_KaipanSdk", FixtureSdk)
    monkeypatch.setitem(sys.modules, "redis", SimpleNamespace(
        Redis=SimpleNamespace(from_url=lambda *_args, **_kwargs: client)))
    timestamps = iter(("2026-10-23T14:41:16+0800", "2026-10-23T14:41:17+0800"))
    def captured_at(format, *args):
        if format == "%Y-%m-%dT%H:%M:%S%z":
            return next(timestamps)
        return real_strftime(format, *args)
    monkeypatch.setattr(kaipan_provider.time, "strftime", captured_at)
    argv = ["--trade-date", "2026-10-23", "--dataset", "hot_plates",
            "--artifact-root", str(tmp_path), "--execute"]

    assert capture_kaipan_plates.main(argv) == 0
    capsys.readouterr()
    after_first = ArtifactCatalog(tmp_path).read()["datasets"]["hot_plates"]["dates"]["2026-10-23"]
    assert len(after_first) == 1

    assert capture_kaipan_plates.main(argv) == 0
    capsys.readouterr()
    versions = ArtifactCatalog(tmp_path).read()["datasets"]["hot_plates"]["dates"]["2026-10-23"]
    assert len(versions) == 2
    manifests = [json.loads(Path(item["manifest"]).read_text(encoding="utf-8")) for item in versions]
    assert len({manifest["fetched_at"] for manifest in manifests}) == 2
