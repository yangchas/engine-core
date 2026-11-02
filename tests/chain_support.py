"""Real captured catalog + manifests of the daily_kline chain (tests/fixtures/real/core_artifacts/c639)."""
import json
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "real" / "core_artifacts" / "c639"
CORE_ROOT = Path("/home/exedev/core-data")
TODAY = "2026-10-30"          # the snapshot was copied on 2026-11-02; the newest closed session in it is 10-30


def snapshot() -> dict:
    return json.loads((FIXTURE / "catalog.json").read_text(encoding="utf-8"))


def read_manifest(entry):
    """Manifest of a catalog entry, read from the copied file at the same relative path."""
    path = FIXTURE / Path(str(entry["manifest"])).relative_to(CORE_ROOT)
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
