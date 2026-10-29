#!/usr/bin/env python3
"""Verify configured Core Redis TTLs with real catalog partitions and isolated keys."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
VENV_PYTHON = Path("/home/exedev/services/engine-next/shared/venv/bin/python")
LIVE_DATA_ROOT = Path("~/core-data").expanduser()
BASE_NAMESPACE = "core:cache:data:"
DATASETS = ("limit_up_pool", "hot_plates", "hot_rank", "daily_kline")
PROTECTED_NAMESPACES = ("cache:", "q2:", "market:", "a2:", "engine_next:", "t1:", "legacy:")


class _NamespacedRedis:
    """Map publisher keys into a unique, expiring Core-only acceptance namespace."""

    def __init__(self, client, namespace: str):
        self.client = client
        self.namespace = namespace
        self.writes: list[str] = []

    def _key(self, key: str) -> str:
        raw = key.decode() if isinstance(key, bytes) else str(key)
        if not raw.startswith(BASE_NAMESPACE):
            raise RuntimeError("acceptance_redis_key_outside_core_data_namespace")
        return self.namespace + raw[len(BASE_NAMESPACE):]

    def get(self, key):
        return self.client.get(self._key(key))

    def ttl(self, key):
        return self.client.ttl(self._key(key))

    def set(self, key, value, ex=None, ttl=None):
        mapped = self._key(key)
        duration = ex if ex is not None else ttl
        if not isinstance(duration, int) or duration <= 0:
            raise RuntimeError("acceptance_redis_write_missing_ttl")
        self.writes.append(mapped)
        return self.client.set(mapped, value, ex=duration)

    def delete(self, _key):
        raise RuntimeError("acceptance_redis_delete_forbidden")


def _load_rehearse_module():
    spec = importlib.util.spec_from_file_location("rehearse_event_for_c609", ROOT / "examples/rehearse_event.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("rehearse_event_module_unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _inside(value: int, low: float, high: float) -> bool:
    return low <= value <= high


def run() -> dict:
    if Path(sys.executable).resolve() != VENV_PYTHON.resolve() and VENV_PYTHON.is_file():
        os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])

    from engine_core.data_pipeline.jobs import JobRunner, RequestBudget
    from engine_core.data_pipeline.registry import get_dataset
    from engine_core.data_pipeline.storage import ArtifactCatalog

    import redis

    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        raise RuntimeError("REDIS_URL_unavailable")
    parsed = urlsplit(redis_url)
    if parsed.scheme != "redis" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.port != 6380:
        raise RuntimeError("REDIS_URL_must_use_guard_proxy_127.0.0.1:6380")
    free_bytes = shutil.disk_usage("/").free
    if free_bytes < 5 * 1024 ** 3:
        raise RuntimeError("root_disk_below_5gb")

    rehearsal = _load_rehearse_module()
    root = Path(tempfile.mkdtemp(prefix="c609", dir="/tmp"))
    data_root = root / "core-data"
    staged = rehearsal.stage_core_data(LIVE_DATA_ROOT, data_root)
    if staged["bytes"] >= 50 * 1024 ** 2 or rehearsal._tree_size_bytes(root) >= 200 * 1024 ** 2:
        raise RuntimeError("acceptance_staging_size_limit_exceeded")

    registry_path = ROOT / "src/engine_core/guardian/datasets.registry.json"
    items = json.loads(registry_path.read_text(encoding="utf-8"))["items"]
    registry = {item["id"]: item for item in items if isinstance(item, dict) and item.get("id")}
    catalog = ArtifactCatalog(data_root)
    token = uuid.uuid4().hex[:16]
    namespace = f"{BASE_NAMESPACE}c609-{token}:"
    guarded = redis.Redis.from_url(redis_url, decode_responses=True,
                                   socket_connect_timeout=2, socket_timeout=10)
    namespaced = _NamespacedRedis(guarded, namespace)
    runner = JobRunner(artifact_root=data_root, redis=namespaced,
                       budget=RequestBudget(min_interval_seconds=0))
    observed = []
    now = datetime.now(timezone.utc).isoformat()

    for name in DATASETS:
        spec = get_dataset(name)
        entry = registry.get(name)
        ttl = spec.redis_ttl_seconds
        if not isinstance(ttl, int) or ttl <= 0:
            raise RuntimeError(f"redis_ttl_missing:{name}")
        if not isinstance(entry, dict) or entry.get("redis_ttl_seconds") != ttl:
            raise RuntimeError(f"registry_ttl_mismatch:{name}")
        source_manifest_path = catalog.locate(name)
        if source_manifest_path is None:
            raise RuntimeError(f"real_catalog_partition_missing:{name}")
        source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
        rows = catalog.read_rows(source_manifest_path)
        if not rows:
            raise RuntimeError(f"real_catalog_partition_empty:{name}")
        source_quality = source_manifest.get("quality") or {}
        expected_count = source_quality.get("expected_count")
        if name == "hot_plates" and not isinstance(expected_count, int):
            if (source_quality.get("status") != "READY" or source_quality.get("coverage") != 1.0
                    or source_quality.get("missing") != 0):
                raise RuntimeError("real_hot_plates_quality_does_not_confirm_complete_partition")
            expected_count = len(rows)
        trade_date = str(source_manifest.get("trade_date") or "")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", trade_date):
            raise RuntimeError(f"real_catalog_trade_date_missing:{name}")

        fetch_result = {
            "rows": rows,
            "provider": str(source_manifest.get("source") or spec.source),
            "available_at": source_manifest.get("available_at"),
            "as_of": source_manifest.get("as_of"),
            "provider_attempts": source_manifest.get("provider_attempts"),
            "field_units": source_manifest.get("field_units"),
            "captured_at": source_manifest.get("captured_at"),
            "raw_sha256": source_manifest.get("raw_sha256"),
            "expected_count": expected_count,
        }
        written_before = len(namespaced.writes)
        with contextlib.redirect_stdout(io.StringIO()):
            result = runner.run(
                name, trade_date, fetch=lambda _day: fetch_result,
                source=spec.source, now=now, as_of=source_manifest.get("as_of"),
                available_at=source_manifest.get("available_at"), publish=True,
            )
        dataset_prefix = namespace + name + ":"
        written = [key for key in namespaced.writes[written_before:] if key.startswith(dataset_prefix)]
        if not written:
            raise RuntimeError(f"publisher_wrote_no_dataset_keys:{name}")
        low, high = ttl * 0.95, ttl * 1.05
        key_results = []
        for key in written:
            value = guarded.get(key)
            remaining = guarded.ttl(key)
            if value is None or remaining <= 0 or not _inside(remaining, low, high):
                raise RuntimeError(f"ttl_readback_out_of_range:{name}:{remaining}")
            key_results.append({"key_shape": key[len(namespace):], "ttl_remaining_seconds": remaining})
        version_key = dataset_prefix + trade_date + ":" + str(result.manifest["version"])
        version_value = guarded.get(version_key)
        if version_value is None:
            raise RuntimeError(f"version_key_readback_missing:{name}")
        published_manifest = json.loads(version_value)
        if published_manifest.get("sha256") != result.manifest.get("sha256"):
            raise RuntimeError(f"version_key_manifest_mismatch:{name}")
        observed.append({"dataset": name, "trade_date": trade_date, "row_count": len(rows),
                         "redis_ttl_seconds": ttl, "keys": key_results})

    violations = [key for key in namespaced.writes
                  if not key.startswith(namespace) or key.startswith(PROTECTED_NAMESPACES)]
    if violations:
        raise RuntimeError("protected_or_unisolated_redis_write_detected")
    return {"status": "PASS", "namespace": namespace, "datasets": observed,
            "protected_namespace_writes": 0, "temporary_data_root": str(data_root),
            "staged_bytes": staged["bytes"], "root_free_bytes": free_bytes}


def main() -> int:
    try:
        result = run()
    except Exception as exc:  # output remains a credential-free PASS/FAIL JSON record
        reason = re.sub(r"redis(?:s)?://[^\s\"']+", "[redis-url redacted]", str(exc))
        result = {"status": "FAIL", "error": type(exc).__name__, "reason": reason[:240]}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
