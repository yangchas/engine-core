"""Immutable local artifacts and an atomic, small JSON catalog."""
from __future__ import annotations

import hashlib
import gzip
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".pending-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


class ArtifactCatalog:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.path = self.root / "catalog.json"

    def read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {"datasets": {}}
        except (OSError, ValueError):
            return {"schema_version": 1, "datasets": {}}

    def commit(self, dataset: str, trade_date: str, rows: Sequence[Mapping[str, Any]],
               manifest: Mapping[str, Any], *, source: str) -> dict[str, Any]:
        payload = b"\n".join(canonical_json(dict(row)) for row in rows) + (b"\n" if rows else b"")
        digest = sha256(payload)
        identity = {"payload_sha256": digest}
        inputs = manifest.get("inputs")
        if isinstance(inputs, Mapping) and inputs:
            identity["inputs"] = dict(inputs)
        version_digest = hashlib.sha256(canonical_json(identity)).hexdigest() if "inputs" in identity else digest
        version = f"sha256-{version_digest[:16]}"
        day_root = self.root / dataset / f"trade_date={trade_date}" / f"source={source}"
        run_dir = day_root / version
        data_path = run_dir / "rows.jsonl.gz"
        manifest_path = run_dir / "manifest.json"
        result = dict(manifest)
        result.update({"version": version, "sha256": digest, "row_count": len(rows),
                       "storage_backend": "artifact", "storage_uri": str(run_dir),
                       "rows_uri": str(data_path), "manifest_uri": str(manifest_path)})
        result["storage_format"] = "jsonl.gz"
        if not manifest_path.is_file():
            _atomic_write(data_path, gzip.compress(payload, mtime=0))
            _atomic_write(manifest_path, canonical_json(result) + b"\n")
        catalog = self.read()
        datasets = catalog.setdefault("datasets", {})
        entry = datasets.setdefault(dataset, {"dates": {}, "latest": None})
        versions = entry.setdefault("dates", {}).setdefault(trade_date, [])
        if version not in [item.get("version") for item in versions]:
            versions.append({"version": version, "manifest": str(manifest_path),
                             "sha256": digest, "row_count": len(rows), "as_of": result.get("as_of")})
        current_latest = entry.get("latest")
        if (result.get("business_status") != "PROVISIONAL"
                and (current_latest is None or trade_date >= current_latest.get("trade_date", ""))):
            entry["latest"] = {"trade_date": trade_date, "version": version}
        _atomic_write(self.path, canonical_json(catalog) + b"\n")
        latest = entry.get("latest")
        if latest and latest.get("version") == version and latest.get("trade_date") == trade_date:
            pointer_path = self.root / dataset / "latest.json"
            _atomic_write(pointer_path, canonical_json({"trade_date": trade_date,
                "version": version, "manifest": str(manifest_path)}) + b"\n")
        return result

    def locate(self, dataset: str, trade_date: str | None = None) -> Path | None:
        catalog = self.read().get("datasets", {})
        entry = catalog.get(dataset, {})
        target_day = trade_date
        if target_day is None:
            target_day = (entry.get("latest") or {}).get("trade_date")
        versions = entry.get("dates", {}).get(target_day, []) if target_day else []
        if not versions:
            return None
        return Path(versions[-1]["manifest"])

    def read_rows(self, manifest_path: Path) -> list[dict[str, Any]]:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        path = Path(manifest["rows_uri"])
        rows = []
        opener = gzip.open if path.suffix == ".gz" else open
        with opener(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                if line.strip(): rows.append(json.loads(line))
        return rows
