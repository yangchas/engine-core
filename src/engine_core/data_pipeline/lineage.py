"""Small manifest lineage helpers for Core derived partitions."""
from __future__ import annotations

import json
from typing import Any

from .storage import ArtifactCatalog


def daily_kline_input(catalog: ArtifactCatalog, day: str) -> dict[str, dict[str, Any]]:
    path = catalog.locate("daily_kline", day)
    if path is None or not path.is_file():
        raise RuntimeError(f"daily_kline_manifest_missing:{day}")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    quality = manifest.get("quality")
    status = quality.get("status") if isinstance(quality, dict) else quality
    return {"daily_kline": {"version": manifest.get("version"),
                            "quality_status": status,
                            "source": manifest.get("source")}}
