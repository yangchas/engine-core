"""Capture quote slices. This command is not connected to scheduling."""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from engine_core.data_pipeline.capture.quote_slice import SOURCES, capture_quote_slice  # noqa: E402
from engine_core.data_pipeline.storage import ArtifactCatalog  # noqa: E402


class _FailedClient:
    def __init__(self, error):
        self.error = error

    def __getattr__(self, _name):
        raise self.error


class _DryRunCatalog:
    def commit(self, dataset, trade_date, rows, manifest, *, source, adapter, context):
        from engine_core.data_pipeline.registry import get_dataset
        from engine_core.data_pipeline.registry.gate import summarize_gate
        gate = summarize_gate(get_dataset(dataset), rows, trade_date=trade_date,
                              manifest=manifest, source=source, adapter=adapter, context=context)
        return {**manifest, "row_count": len(rows), "version": "DRY_RUN", "gate": gate}


def _redis_client():
    url = os.environ.get("REDIS_URL")
    if not url:
        raise RuntimeError("REDIS_URL is required")
    import redis
    return redis.Redis.from_url(url, socket_timeout=3, socket_connect_timeout=3)


def _td_client():
    config = os.environ.get("TD_CONFIG")
    if not config:
        raise RuntimeError("TD_CONFIG is required")
    import taos
    return taos.connect(database="market_data1", config=config)


def _connect(factory):
    started = time.perf_counter()
    try:
        return factory(), time.perf_counter() - started
    except Exception as exc:
        return _FailedClient(exc), time.perf_counter() - started


def _safe_message(value):
    return re.sub(r"(://)[^/@\s]+@", r"\1***@", str(value))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="trade date in YYYY-MM-DD form")
    parser.add_argument("--tag", required=True, choices=("0920", "0924", "0925"))
    parser.add_argument("--sources", default=",".join(SOURCES), help="comma-separated source names")
    parser.add_argument("--core-data-root", type=Path,
                        default=Path(os.environ.get("CORE_DATA_ROOT", Path.home() / "core-data")))
    parser.add_argument("--dry-run", action="store_true", help="read sources and report; do not commit")
    args = parser.parse_args(argv)
    sources = tuple(item.strip() for item in args.sources.split(",") if item.strip())
    if not sources or len(set(sources)) != len(sources) or any(item not in SOURCES for item in sources):
        parser.error(f"--sources values must be unique members of: {', '.join(SOURCES)}")

    clients, connect_seconds = {}, {}
    factories = {"redis_q2": _redis_client, "td_auction_snapshot_v2": _td_client}
    try:
        for source in sources:
            clients[source], connect_seconds[source] = _connect(factories[source])
        result = capture_quote_slice(
            args.date, args.tag, redis_client=clients.get("redis_q2"),
            td_client=clients.get("td_auction_snapshot_v2"),
            catalog=_DryRunCatalog() if args.dry_run else ArtifactCatalog(args.core_data_root),
            sources=sources)
        print(f"MODE {'DRY_RUN' if args.dry_run else 'COMMIT'}")
        for source in sources:
            if source in result["committed"]:
                item = result["committed"][source]
                gate = item.get("gate", {})
                quality = item.get("quality", {})
                elapsed = result["timings_seconds"][source] + connect_seconds[source]
                print(f"SOURCE {source} ROWS {item.get('row_count')} GATE {gate.get('gate_status')} "
                      f"FLAG_COUNTS {gate.get('flags_by_field', {})} COVERAGE {quality.get('coverage')} "
                      f"VERSION {item.get('version')} SECONDS {elapsed:.3f}")
            elif source in result["failed"]:
                failure = result["failed"][source]
                print(f"FAILED {source} {failure['error_type']} {_safe_message(failure['message'])}")
        print(f"TRADE_DATE {args.date} TAG {args.tag}")
        return 0 if result["committed"] else 1
    finally:
        for client in clients.values():
            close = getattr(client, "close", None)
            if callable(close):
                close()


if __name__ == "__main__":
    raise SystemExit(main())
