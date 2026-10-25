"""Immutable local artifacts and an atomic, small JSON catalog."""
from __future__ import annotations

import fcntl
import hashlib
import gzip
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_DAILY_KLINE_PROVIDERS = {"baostock", "wencai", "q2_final"}
_DAILY_KLINE_FIELDS = ("open", "high", "low", "close", "pre_close", "pct_chg", "amount_yuan", "volume")


def _daily_kline_time(value: Any) -> str | None:
    if value in (None, "", "UNKNOWN"):
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_SHANGHAI)
    return parsed.astimezone(_SHANGHAI).isoformat()


def _daily_kline_symbol(value: Any) -> str:
    symbol = str(value or "").strip()
    if "." in symbol:
        symbol = symbol.rsplit(".", 1)[-1]
    return symbol.zfill(6) if symbol.isdigit() else symbol


def _prepare_daily_kline_manifest(catalog: "ArtifactCatalog", trade_date: str,
                                  rows: Sequence[Mapping[str, Any]], manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Fill authoritative provider, active-universe quality, status, and local timestamps."""
    from .registry.daily_kline import classify_daily_kline

    result = dict(manifest)
    provider = result.get("provider")
    source = result.get("source")
    if provider not in _DAILY_KLINE_PROVIDERS and provider != "UNKNOWN":
        provider = source if source in _DAILY_KLINE_PROVIDERS else (
            "q2_final" if source == "legacy_q2_final" else "UNKNOWN")
    result["provider"] = provider

    quality = dict(result.get("quality") or {})
    universe_path = catalog.locate("symbol_universe", trade_date)
    row_symbols = {_daily_kline_symbol(row.get("symbol", row.get("code"))) for row in rows}
    row_symbols.discard("")
    quality["row_count"] = len(rows)
    if universe_path is None:
        quality.update({"expected_count": None, "expected_basis": "universe_unavailable",
                        "suspended_in_universe": None, "suspended_without_bar": None,
                        "coverage": None, "missing": None, "partial": True,
                        "status": "UNKNOWN"})
    else:
        universe_rows = catalog.read_rows(universe_path)
        by_symbol = {_daily_kline_symbol(row.get("symbol", row.get("code"))): row
                     for row in universe_rows}
        by_symbol.pop("", None)
        suspended = {symbol for symbol, row in by_symbol.items()
                     if str(row.get("trade_status", "")).strip() == "0"}
        active = set(by_symbol) - suspended
        active_missing = active - row_symbols
        coverage = len(active & row_symbols) / len(active) if active else None
        quality.update({"expected_count": len(active), "expected_basis": "universe_active",
                        "suspended_in_universe": len(suspended),
                        "suspended_without_bar": len(suspended - row_symbols),
                        "coverage": coverage, "missing": len(active_missing) if active else None,
                        "partial": coverage is None or coverage < 1.0,
                        "status": "READY" if coverage is not None and coverage >= 0.98 else "PARTIAL"})
    # Derive the eight required field states from the committed rows. Missing
    # values are excused only when every affected symbol is suspended in the
    # same-date universe; declared provider labels cannot override this check.
    universe_suspended = suspended if universe_path is not None else set()
    completeness: dict[str, str] = {}
    for field in _DAILY_KLINE_FIELDS:
        missing_symbols = {_daily_kline_symbol(row.get("symbol", row.get("code")))
                           for row in rows if row.get(field) is None}
        if rows and not missing_symbols:
            status = "COMPLETE"
        elif missing_symbols and universe_path is not None and missing_symbols <= universe_suspended:
            status = "SUSPENDED_ONLY"
        elif not any(row.get(field) is not None for row in rows):
            status = "MISSING"
        else:
            status = "PARTIAL"
        completeness[field] = status
    quality["field_completeness"] = completeness
    result["field_completeness"] = completeness
    result["quality"] = quality
    result["business_status"] = classify_daily_kline(provider, quality.get("coverage"), completeness)

    attempts = result.get("provider_attempts")
    normalized_attempts = []
    if isinstance(attempts, (list, tuple)):
        for item in attempts:
            if not isinstance(item, Mapping):
                continue
            attempt_provider = item.get("provider")
            attempt_provider = attempt_provider if attempt_provider in _DAILY_KLINE_PROVIDERS else "UNKNOWN"
            attempt_result = item.get("result")
            if attempt_result is None:
                old_status = item.get("status")
                attempt_result = "ACCEPTED" if old_status == "READY" else "ERROR" if old_status == "PROVIDER_UNAVAILABLE" else "UNKNOWN"
            elapsed = item.get("elapsed_ms")
            if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)):
                elapsed = None
            normalized = {"provider": attempt_provider, "result": str(attempt_result),
                          "attempted_at": _daily_kline_time(item.get("attempted_at")),
                          "elapsed_ms": elapsed,
                          "reason": str(item.get("reason") or "")}
            for key in ("login_ms", "query_ms", "error_code", "chain_result", "call_result", "coverage"):
                if key in item:
                    normalized[key] = item[key]
            normalized_attempts.append(normalized)
    if not normalized_attempts:
        normalized_attempts.append({"provider": provider, "result": "UNKNOWN",
                                    "attempted_at": None, "elapsed_ms": None,
                                    "reason": "provider_attempts_unavailable_at_writer"})
    result["provider_attempts"] = normalized_attempts

    for key in ("available_at", "declared_available_at", "fetched_at", "as_of"):
        if key in result:
            result[key] = _daily_kline_time(result[key])
    return result


def available_at_trust(manifest: Mapping[str, Any]) -> str:
    return "MEASURED" if manifest.get("committed_at") else "DECLARED_LEGACY"


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_degraded_symbol_universe(entry: Mapping[str, Any], root: Path) -> bool:
    if entry.get("source") == "carried_forward":
        return True
    manifest_ref = entry.get("manifest")
    if not isinstance(manifest_ref, str):
        return False
    path = Path(manifest_ref)
    if not path.is_absolute():
        path = root / path
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    tier = manifest.get("tier") if isinstance(manifest, dict) else None
    return type(tier) is int and tier >= 1 and manifest.get("degraded") is True


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


@contextmanager
def _catalog_lock(root: Path):
    """Serialize catalog.json read-modify-write across processes (guardian jobs, reports, node writes)."""
    root.mkdir(parents=True, exist_ok=True)
    with open(root / ".catalog.lock", "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


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
               manifest: Mapping[str, Any], *, source: str, authority: str = "LIVE",
               clock: Any = None, adapter: str | None = None,
               context: Mapping[str, Any] | None = None) -> dict[str, Any]:
        prepared_manifest = (_prepare_daily_kline_manifest(self, trade_date, rows, manifest)
                             if dataset == "daily_kline" else dict(manifest))
        gate = None
        from .registry import DATASETS
        spec = DATASETS.get(dataset)
        if spec is not None and spec.fields and spec.gate_mode != "off":
            from .registry.gate import summarize_gate
            gate = summarize_gate(spec, rows, trade_date=trade_date, manifest=prepared_manifest,
                                  source=source, adapter=adapter, context=context,
                                  sample_rows=rows[:200])
        payload = b"\n".join(canonical_json(dict(row)) for row in rows) + (b"\n" if rows else b"")
        digest = sha256(payload)
        identity = {"payload_sha256": digest}
        if "node_fingerprint" in prepared_manifest:
            identity["node_fingerprint"] = prepared_manifest["node_fingerprint"]
        inputs = prepared_manifest.get("inputs")
        if isinstance(inputs, Mapping) and inputs:
            identity["inputs"] = dict(inputs)
        version_digest = hashlib.sha256(canonical_json(identity)).hexdigest() if "inputs" in identity else digest
        version = f"sha256-{version_digest[:16]}"
        day_root = self.root / dataset / f"trade_date={trade_date}" / f"source={source}"
        run_dir = day_root / version
        data_path = run_dir / "rows.jsonl.gz"
        manifest_path = run_dir / "manifest.json"
        committed_dt = (clock or (lambda: datetime.now(timezone.utc)))()
        if committed_dt.tzinfo is None:
            committed_dt = committed_dt.replace(tzinfo=timezone.utc)
        committed_dt = committed_dt.astimezone(timezone.utc)
        committed_at = committed_dt.isoformat().replace("+00:00", "Z")
        available_at = committed_dt.astimezone(_SHANGHAI).isoformat() if dataset == "daily_kline" else committed_at
        result = dict(prepared_manifest)
        declared = result.pop("available_at", None) or result.pop("declared_available_at", None)
        declared_authority = result.pop("authority", None)
        result["committed_at"] = committed_at
        result["available_at"] = available_at
        if declared is not None and declared != available_at:
            result["declared_available_at"] = _daily_kline_time(declared) if dataset == "daily_kline" else declared
        result["authority"] = authority
        if declared_authority is not None and declared_authority != authority:
            result["declared_authority"] = declared_authority
        captured_at = result.get("captured_at")
        if captured_at:
            try:
                captured = datetime.fromisoformat(str(captured_at).replace("Z", "+00:00"))
                commit_dt = datetime.fromisoformat(committed_at.replace("Z", "+00:00"))
                if captured.tzinfo is None:
                    captured = captured.replace(tzinfo=timezone.utc)
                if captured > commit_dt:
                    result["captured_at"] = committed_at
            except ValueError:
                pass
        else:
            result["captured_at"] = None
        result["business_date"] = trade_date
        result["trade_date"] = trade_date
        result["source_asof"] = result.get("source_asof") or result.get("as_of") or "UNKNOWN"
        result.update({"version": version, "sha256": digest, "row_count": len(rows),
                       "storage_backend": "artifact", "storage_uri": str(run_dir),
                       "rows_uri": str(data_path), "manifest_uri": str(manifest_path)})
        result["storage_format"] = "jsonl.gz"
        if gate is not None:
            result["gate"] = gate
        if not manifest_path.is_file():
            _atomic_write(data_path, gzip.compress(payload, mtime=0))
            _atomic_write(manifest_path, canonical_json(result) + b"\n")
        else:
            try:
                existing = json.loads(manifest_path.read_text(encoding="utf-8"))
                if gate is not None:
                    existing["gate"] = gate
                    _atomic_write(manifest_path, canonical_json(existing) + b"\n")
                result = existing
            except (OSError, ValueError):
                pass
        with _catalog_lock(self.root):
            catalog = self.read()
            datasets = catalog.setdefault("datasets", {})
            entry = datasets.setdefault(dataset, {"dates": {}, "latest": None})
            versions = entry.setdefault("dates", {}).setdefault(trade_date, [])
            if str(manifest_path) not in [item.get("manifest") for item in versions]:
                catalog_version = {"version": version, "manifest": str(manifest_path),
                                 "sha256": digest, "row_count": len(rows), "as_of": result.get("as_of"),
                                 "source": source, "business_status": result.get("business_status"),
                                 **{key: result[key] for key in ("available_at", "committed_at", "authority")
                                    if key in result},
                                 **({"node_fingerprint": result["node_fingerprint"]}
                                    if "node_fingerprint" in result else {})}
                if gate is not None:
                    catalog_version["gate_status"] = gate["gate_status"]
                versions.append(catalog_version)
            else:
                for item in versions:
                    if item.get("manifest") == str(manifest_path):
                        if "node_fingerprint" in result:
                            item["node_fingerprint"] = result["node_fingerprint"]
                        if gate is not None:
                            item["gate_status"] = gate["gate_status"]
            current_latest = entry.get("latest")
            if (result.get("business_status") != "PROVISIONAL"
                    and not (spec is not None and spec.gate_mode == "enforce"
                             and gate is not None and gate["gate_status"] == "FAIL")
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
        if dataset == "symbol_universe" and len(versions) > 1:
            for version in reversed(versions):
                if not _is_degraded_symbol_universe(version, self.root):
                    return Path(version["manifest"])
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
