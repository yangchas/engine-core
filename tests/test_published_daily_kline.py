"""find_published_baostock_manifest: scans every catalog version (identical file in the C631 and C632 branches)."""
import json

import pytest

TARGET = "2026-10-30"


class _StubCatalog:
    """Catalog with several versions of TARGET, like the real ones (legacy chain, wencai close, Baostock fill)."""

    def __init__(self, root, manifests):
        self.root = root
        self.versions = []
        for index, manifest in enumerate(manifests):
            path = root / f"m{index}.json"
            path.write_text(json.dumps(manifest))
            self.versions.append({"manifest": str(path)})

    def read(self):
        return {"datasets": {"daily_kline": {"dates": {TARGET: self.versions}}}}


def _manifest(provider="baostock", status="FORMAL", coverage=1.0):
    return {"provider": provider, "business_status": status, "quality": {"coverage": coverage}}


@pytest.mark.parametrize("manifests,expected", [
    ([_manifest()], True),
    ([_manifest(status="PROVISIONAL", coverage=0.9954)], True),
    ([_manifest(status="PROVISIONAL", coverage=0.98)], False),
    ([_manifest(status="PARTIAL", coverage=0.999)], False),
    ([_manifest(provider="wencai", status="PROVISIONAL")], False),
    ([_manifest(provider=None, status="FORMAL", coverage=0.9996)], False),
    ([], False),
    # the real 2026-10-19 shape: a legacy FORMAL outranks the Baostock PROVISIONAL in locate(), not here
    ([_manifest(provider=None, status="FORMAL", coverage=0.9996),
      _manifest(status="PROVISIONAL", coverage=0.995)], True),
    # the real 15:05 shape: a wencai PROVISIONAL with coverage 1.0 outranks a Baostock one with 0.9958
    ([_manifest(provider="wencai", status="PROVISIONAL", coverage=1.0),
      _manifest(status="PROVISIONAL", coverage=0.9958)], True),
], ids=["formal", "provisional-enough", "provisional-low", "partial", "wencai", "legacy-no-provider", "absent",
        "legacy-formal-outranks", "wencai-outranks"])
def test_published_means_any_baostock_version_with_enough_coverage(tmp_path, manifests, expected):
    from engine_core.data_pipeline.published_daily_kline import find_published_baostock_manifest

    found = find_published_baostock_manifest(_StubCatalog(tmp_path, manifests), TARGET, 0.995)
    assert (found is not None) is expected


