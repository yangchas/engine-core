"""Has a day's Baostock daily_kline been published? Scans every catalog version, never ``locate``.

``ArtifactCatalog.locate`` returns the single best version by (status rank, coverage, provider tier). A legacy
chain-labelled FORMAL (no provider) or a wencai PROVISIONAL with coverage 1.0 outranks a Baostock PROVISIONAL with
coverage 0.995, so asking ``locate`` would report "not published" for a day the TD fill has published, forever.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def find_published_baostock_manifest(catalog: Any, day: str, min_coverage: float = 0.995) -> dict[str, Any] | None:
    """The newest Baostock manifest of ``day`` that is FORMAL or PROVISIONAL with coverage >= ``min_coverage``.

    PROVISIONAL with enough coverage is a property of the data (a few rows with empty volume/amount) and never
    becomes FORMAL, so it counts as published; only "no such manifest" means the fill still has to publish.
    """
    entry = catalog.read().get("datasets", {}).get("daily_kline", {})
    root = Path(getattr(catalog, "root", "."))
    found = None
    for version in entry.get("dates", {}).get(day, []):
        path = Path(str(version.get("manifest") or ""))
        if not path.is_absolute():
            path = root / path
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(manifest, dict) or str(manifest.get("provider") or "").lower() != "baostock":
            continue
        coverage = (manifest.get("quality") or {}).get("coverage")
        if (manifest.get("business_status") in {"FORMAL", "PROVISIONAL"} and isinstance(coverage, (int, float))
                and not isinstance(coverage, bool) and float(coverage) >= min_coverage):
            found = manifest  # versions are appended in commit order: keep the newest qualifying one
    return found
