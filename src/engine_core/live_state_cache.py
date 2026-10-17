"""Compressed, date/checkpoint keyed cache for immutable TD state snapshots."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
from functools import lru_cache
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


SCHEMA = "CoreLiveStatesV1"
ALGORITHM_VERSION = "states-at-v2"
DATA_SOURCE = "market_data1.stock_tick_v2"


def _canonical(states: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(symbol).zfill(6): dict(row) for symbol, row in sorted(states.items(), key=lambda item: str(item[0]))}


def _paths(cutoff: datetime, sha: str) -> tuple[Path, Path]:
    root = Path(os.getenv("CORE_SLICE_ROOT") or os.getenv("CORE_DATA_ROOT", str(Path.home() / "core-data"))).expanduser()
    directory = root / "live_states" / cutoff.strftime("%Y-%m-%d") / cutoff.strftime("%H%M%S")
    return directory / f"{sha}.json.gz", directory / f"{sha}.manifest.json"


@lru_cache(maxsize=2)
def _redis_client(url: str | None):
    if not url:
        return None
    try:
        import redis
        return redis.Redis.from_url(url, decode_responses=False, socket_connect_timeout=1,
                                    socket_timeout=1, health_check_interval=30)
    except Exception:
        return None


MIN_ROWS = 4_000  # full-market checkpoint floor; must match the writer in live_session.states_at


def _key(cutoff: datetime) -> str:
    return f"core:cache:states_at:{cutoff:%Y-%m-%d}:{cutoff:%H%M%S}"


def _decode(blob: bytes, cutoff: datetime) -> dict[str, dict[str, Any]] | None:
    try:
        envelope = json.loads(gzip.decompress(blob))
        states = envelope["states"]
        if not isinstance(states, dict) or len(states) < MIN_ROWS:
            return None  # empty/partial snapshots are never a valid cache hit
        canonical = json.dumps(states, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        if (envelope.get("schema") != SCHEMA or envelope.get("algorithm_version") != ALGORITHM_VERSION
                or envelope.get("data_source") != DATA_SOURCE or not envelope.get("fetched_at")
                or envelope.get("cutoff") != cutoff.isoformat(sep=" ")
                or envelope.get("data_through") != cutoff.isoformat(sep=" ")
                or envelope.get("symbol_count") != len(states)
                or envelope.get("states_sha256") != hashlib.sha256(canonical).hexdigest()):
            return None
        return states
    except (OSError, ValueError, TypeError, KeyError, EOFError):
        return None


def read(cutoff: datetime) -> dict[str, dict[str, Any]] | None:
    client = _redis_client(os.getenv("REDIS_URL"))
    if client is not None:
        try:
            blob = client.get(_key(cutoff))
            cached = _decode(blob, cutoff) if blob else None
            if cached is not None:
                return cached
        except Exception:
            pass
    directory = _paths(cutoff, "")[0].parent
    try:
        manifests = sorted(directory.glob("*.manifest.json"), key=lambda path: path.stat().st_mtime_ns,
                           reverse=True)
        for manifest_path in manifests:
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                data_path = manifest_path.with_name(manifest_path.name.removesuffix(".manifest.json") + ".json.gz")
                blob = data_path.read_bytes()
                if hashlib.sha256(blob).hexdigest() != manifest.get("blob_sha256"):
                    continue
                states = _decode(blob, cutoff)
                if states is not None:
                    if client is not None:
                        try:
                            client.set(_key(cutoff), blob, ex=7 * 86400)
                        except Exception:
                            pass
                    return states
            except (OSError, ValueError, TypeError):
                continue
    except OSError:
        pass
    return None


def write(cutoff: datetime, states: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    normalized = _canonical(states)
    raw = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    sha = hashlib.sha256(raw).hexdigest()
    envelope = {"schema": SCHEMA, "algorithm_version": ALGORITHM_VERSION, "data_source": DATA_SOURCE,
                "cutoff": cutoff.isoformat(sep=" "),
                "data_through": cutoff.isoformat(sep=" "), "fetched_at": datetime.now().isoformat(sep=" "),
                "symbol_count": len(normalized), "states_sha256": sha, "states": normalized}
    blob = gzip.compress(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode(), mtime=0)
    data_path, manifest_path = _paths(cutoff, sha)
    data_path.parent.mkdir(parents=True, exist_ok=True)
    if not data_path.exists():
        temporary = data_path.with_suffix(data_path.suffix + ".tmp")
        temporary.write_bytes(blob)
        try:
            os.link(temporary, data_path)
        except FileExistsError:
            pass
        finally:
            temporary.unlink(missing_ok=True)
    manifest = {"schema": SCHEMA, "algorithm_version": ALGORITHM_VERSION, "data_source": DATA_SOURCE,
                "trade_date": cutoff.date().isoformat(),
                "cutoff": cutoff.isoformat(sep=" "), "fetched_at": envelope["fetched_at"],
                "data_through": envelope["data_through"], "symbol_count": len(normalized),
                "states_sha256": sha, "blob_sha256": hashlib.sha256(blob).hexdigest()}
    if not manifest_path.exists():
        temporary_manifest = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
        temporary_manifest.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        try:
            os.link(temporary_manifest, manifest_path)
        except FileExistsError:
            pass
        finally:
            temporary_manifest.unlink(missing_ok=True)
    client = _redis_client(os.getenv("REDIS_URL"))
    if client is not None:
        try:
            client.set(_key(cutoff), blob, ex=7 * 86400)
        except Exception:
            pass
    return {**manifest, "compressed_bytes": len(blob)}
