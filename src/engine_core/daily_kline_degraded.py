"""Read-only q2/WenCai daily bars for the 15:05 degraded capture."""
from __future__ import annotations

import importlib
import json
import os
import socket
import ssl
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

from .data_pipeline.providers.daily_kline_chain import Q2FinalProvider, WencaiDailyProvider
from .data_pipeline.registry.daily_kline import classify_daily_kline
from .data_pipeline.storage import ArtifactCatalog

SHANGHAI = ZoneInfo("Asia/Shanghai")
FIELDS = ("open", "high", "low", "close", "pre_close", "pct_chg", "amount_yuan", "volume")


class _ReadOnlyRedisPipeline:
    def __init__(self, client: "ReadOnlyRedis"):
        self.client = client
        self.keys: list[str] = []

    def hgetall(self, key: str) -> None:
        self.keys.append(key)

    def execute(self) -> list[dict[bytes, bytes]]:
        results = []
        for key in self.keys:
            flat = self.client.command("HGETALL", key) or []
            results.append({flat[i]: flat[i + 1] for i in range(0, len(flat), 2)})
        return results


class ReadOnlyRedis:
    """Small RESP2 read-only client, using only SMEMBERS and HGETALL."""

    def __init__(self, url: str):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"redis", "rediss"} or not parsed.hostname:
            raise ValueError("REDIS_URL must use redis:// or rediss://")
        port = parsed.port or (6380 if parsed.scheme == "rediss" else 6379)
        raw = socket.create_connection((parsed.hostname, port), timeout=5)
        if parsed.scheme == "rediss":
            raw = ssl.create_default_context().wrap_socket(raw, server_hostname=parsed.hostname)
        self.socket = raw
        self.socket.settimeout(10)
        self.reader = self.socket.makefile("rb")
        if parsed.username is not None or parsed.password is not None:
            password = urllib.parse.unquote(parsed.password or "")
            if parsed.username is None:
                self.command("AUTH", password)
            else:
                self.command("AUTH", urllib.parse.unquote(parsed.username), password)
        if parsed.path.strip("/"):
            self.command("SELECT", parsed.path.strip("/"))

    def _line(self) -> bytes:
        value = self.reader.readline()
        if not value.endswith(b"\r\n"):
            raise RuntimeError("redis protocol error")
        return value[:-2]

    def _read(self) -> Any:
        prefix = self.reader.read(1)
        if prefix == b"+":
            return self._line()
        if prefix == b"-":
            self._line()
            raise RuntimeError("redis command failed")
        if prefix == b":":
            return int(self._line())
        if prefix == b"$":
            size = int(self._line())
            if size < 0:
                return None
            value = self.reader.read(size)
            if self.reader.read(2) != b"\r\n":
                raise RuntimeError("redis protocol error")
            return value
        if prefix == b"*":
            size = int(self._line())
            return None if size < 0 else [self._read() for _ in range(size)]
        raise RuntimeError("redis protocol error")

    def command(self, *args: str) -> Any:
        payload = b"*%d\r\n" % len(args)
        for arg in args:
            encoded = arg.encode("utf-8")
            payload += b"$%d\r\n" % len(encoded) + encoded + b"\r\n"
        self.socket.sendall(payload)
        return self._read()

    def smembers(self, key: str) -> list[bytes]:
        return self.command("SMEMBERS", key) or []

    def pipeline(self, transaction: bool = False) -> _ReadOnlyRedisPipeline:
        return _ReadOnlyRedisPipeline(self)

    def close(self) -> None:
        self.reader.close()
        self.socket.close()


def compose_degraded_rows(
    q2_rows: Sequence[Mapping[str, Any]],
    wencai_rows: Sequence[Mapping[str, Any]],
    *,
    expected_symbols: Sequence[str] | None = None,
    expected_count: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Prefer q2 values and let WenCai fill null fields, retaining field lineage."""
    q2_by_symbol = {str(row.get("symbol", "")).zfill(6): dict(row)
                    for row in q2_rows if row.get("symbol")}
    wencai_by_symbol = {str(row.get("symbol", "")).zfill(6): dict(row)
                        for row in wencai_rows if row.get("symbol")}
    symbols = sorted(q2_by_symbol.keys() | wencai_by_symbol.keys())
    rows: list[dict[str, Any]] = []
    for symbol in symbols:
        q2 = q2_by_symbol.get(symbol, {})
        wencai = wencai_by_symbol.get(symbol, {})
        row: dict[str, Any] = {"symbol": symbol, "date": q2.get("date") or wencai.get("date")}
        field_sources: dict[str, str] = {}
        for field in FIELDS:
            if q2.get(field) is not None:
                row[field] = q2[field]
                field_sources[field] = "q2_final"
            elif wencai.get(field) is not None:
                row[field] = wencai[field]
                field_sources[field] = "wencai"
            else:
                row[field] = None
        used_sources = list(dict.fromkeys(field_sources.values()))
        row["source"] = "+".join(used_sources)
        row["field_sources"] = field_sources
        row["degraded"] = True
        rows.append(row)

    expected = len(expected_symbols) if expected_symbols is not None else expected_count
    present = {row["symbol"] for row in rows}
    if expected_symbols is not None:
        missing_symbols = sorted(set(str(symbol).zfill(6) for symbol in expected_symbols) - present)
    else:
        missing_symbols = []
    missing = len(missing_symbols) if expected_symbols is not None else (
        max(0, int(expected) - len(present)) if expected is not None else None)
    coverage = min(1.0, len(present) / expected) if expected else None
    return rows, {
        "status": "PARTIAL",
        "coverage": coverage,
        "row_count": len(rows),
        "expected_count": expected,
        "missing": missing,
        "missing_symbols": missing_symbols if expected_symbols is not None else None,
    }


def _load_adapter(spec: str | None) -> Callable[..., Any] | None:
    if not spec:
        return None
    module_name, separator, function_name = spec.partition(":")
    if not separator:
        raise ValueError("--wencai-adapter must be module:function")
    factory = getattr(importlib.import_module(module_name), function_name)
    adapter = factory()
    if isinstance(adapter, Mapping):
        adapter = adapter.get("wencai_daily_kline")
    if not callable(adapter):
        raise ValueError("WenCai adapter must return a daily-kline callable")
    return adapter


def _expected_symbols(root: Path, trade_date: str) -> list[str] | None:
    from .data_pipeline.reader import load_dataset

    dataset = load_dataset("symbol_universe", trade_date, prefer="file", catalog=ArtifactCatalog(root))
    if dataset is None:
        return None
    return sorted({str(row.get("symbol", row.get("code", ""))).zfill(6)
                   for row in dataset.rows if row.get("symbol") or row.get("code")})


def capture(
    trade_date: str,
    *,
    artifact_root: str | Path,
    redis_client: Any,
    wencai_fetch: Callable[..., Any] | None = None,
    now: datetime | None = None,
    write: bool = False,
) -> dict[str, Any]:
    current = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    root = Path(artifact_root)
    expected_symbols = _expected_symbols(root, trade_date)
    expected_count = len(expected_symbols) if expected_symbols is not None else None
    q2_provider = Q2FinalProvider(redis_client)
    q2_started = time.monotonic()
    q2_payload = q2_provider.fetch(trade_date, now=current)
    q2_elapsed_ms = round((time.monotonic() - q2_started) * 1000, 3)
    q2_rows = q2_payload.get("rows", [])

    wencai_payload: dict[str, Any] = {"status": "PROVIDER_UNAVAILABLE", "rows": [],
                                      "reason": "wencai_adapter_not_configured"}
    wencai_elapsed_ms = 0.0
    if wencai_fetch is not None:
        provider = WencaiDailyProvider(wencai_fetch, state_path=None,
                                       expected_count=expected_count)
        wencai_started = time.monotonic()
        wencai_payload = provider.fetch(trade_date, now=current)
        wencai_elapsed_ms = round((time.monotonic() - wencai_started) * 1000, 3)
    wencai_rows = wencai_payload.get("rows", [])
    rows, quality = compose_degraded_rows(q2_rows, wencai_rows,
                                          expected_symbols=expected_symbols,
                                          expected_count=expected_count or q2_payload.get("expected_count"))
    sources = sorted({source for row in rows for source in row["field_sources"].values()})
    provider_sources = {"q2_final" if source == "legacy_q2_final" else source for source in sources}
    provider_name = next(iter(provider_sources)) if len(provider_sources) == 1 and next(iter(provider_sources)) in {
        "baostock", "wencai", "q2_final"} else "UNKNOWN"
    available_at = current.isoformat()
    field_completeness = {
        field: ("COMPLETE" if rows and all(row.get(field) is not None for row in rows) else
                "MISSING" if not any(row.get(field) is not None for row in rows) else "PARTIAL")
        for field in FIELDS
    }
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "dataset": "daily_kline",
        "trade_date": trade_date,
        "provider": provider_name,
        "source": ">".join(sources) if sources else "none",
        "provider_chain": "q2_final>wencai",
        "source_dependency": "legacy_redis_q2+independent_wencai",
        "available_at": available_at,
        "fetched_at": available_at,
        "as_of": available_at,
        "quality": {**quality, "partial": True, "field_completeness": field_completeness,
                    "field_units": {"open/high/low/close/pre_close": "yuan",
                                    "pct_chg": "percent", "amount_yuan": "yuan",
                                    "volume": "UNKNOWN"}},
        "degraded": True,
        "degraded_label": "DEGRADED:q2_final>wencai",
        "status": "DEGRADED:q2_final>wencai",
        "business_status": classify_daily_kline(provider_name, quality.get("coverage"), field_completeness),
        "provider_attempts": [
            {"provider": "q2_final",
             "result": "ACCEPTED" if q2_payload.get("status") != "PROVIDER_UNAVAILABLE" and q2_rows else "ERROR",
             "attempted_at": current.isoformat(), "elapsed_ms": q2_elapsed_ms,
             "reason": q2_payload.get("reason") or ("rows_received" if q2_rows else "no_rows"),
             "status": q2_payload.get("status", "READY"), "row_count": len(q2_rows),
             "stale_count": q2_payload.get("stale_count", 0)},
            {"provider": "wencai",
             "result": "ACCEPTED" if wencai_payload.get("status") != "PROVIDER_UNAVAILABLE" and wencai_rows else "ERROR",
             "attempted_at": current.isoformat(), "elapsed_ms": wencai_elapsed_ms,
             "reason": wencai_payload.get("reason") or ("rows_received" if wencai_rows else "no_rows"),
             "status": wencai_payload.get("status", "READY"), "row_count": len(wencai_rows)},
        ],
        "field_completeness": field_completeness,
        "field_units": {"open/high/low/close/pre_close": "yuan", "pct_chg": "percent",
                        "amount_yuan": "yuan", "volume": "UNKNOWN"},
    }
    if write:
        manifest = ArtifactCatalog(root).commit("daily_kline", trade_date, rows, manifest,
                                                source=manifest["source"])
    return {"manifest": manifest, "rows": rows, "quality": manifest["quality"]}


def main(argv: Sequence[str] | None = None, *, redis_client: Any = None,
         wencai_fetch: Callable[..., Any] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True)
    parser.add_argument("--artifact-root", default=os.getenv("CORE_DATA_ROOT", "~/core-data"))
    parser.add_argument("--wencai-adapter", help="read-only adapter factory as module:function")
    parser.add_argument("--write", action="store_true", help="append a provisional catalog version")
    args = parser.parse_args(argv)
    try:
        datetime.strptime(args.date, "%Y-%m-%d")
        adapter = wencai_fetch or _load_adapter(args.wencai_adapter)
        client = redis_client
        owns_client = client is None
        if client is None:
            redis_url = os.getenv("REDIS_URL")
            if not redis_url:
                raise RuntimeError("REDIS_URL is required for the read-only q2 source")
            client = ReadOnlyRedis(redis_url)
        try:
            result = capture(args.date, artifact_root=Path(args.artifact_root).expanduser(),
                             redis_client=client, wencai_fetch=adapter, write=args.write)
        finally:
            if owns_client and isinstance(client, ReadOnlyRedis):
                client.close()
        manifest = result["manifest"]
        print(json.dumps({"job": "daily_kline_degraded",
                          "mode": "WRITE" if args.write else "DRY_RUN",
                          "date": args.date,
                          "source": manifest["source"],
                          "available_at": manifest["available_at"],
                          "quality": manifest["quality"],
                          "row_count": manifest.get("row_count", len(result["rows"])),
                          "version": manifest.get("version")}, sort_keys=True))
        return 0
    except Exception as exc:
        parser.exit(2, f"capture_daily_kline_degraded: {type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
