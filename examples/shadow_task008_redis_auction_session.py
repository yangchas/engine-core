"""Read-only shadow summaries for auction anchors from sealed or live Redis Q2."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from engine_core import AuctionAnchorSessionV1, FreshnessPolicy, RedisQ2ProjectionAdapter  # noqa: E402
from engine_core.auction_shadow_report import build_shadow_auction_report  # noqa: E402
from engine_core.previous_day_limit_replay import previous_day_limit_pool_from_artifact  # noqa: E402

ANCHOR_FIELDS = {"0920": "a20", "0924": "a24", "0925": "a25"}
SOURCE_LAYER = "redis_q2_shadow"


class ReadOnlyQ2Client:
    """Restrict a Redis-like client to the two Q2 projection read operations."""

    __slots__ = ("__client",)

    def __init__(self, client: Any) -> None:
        object.__setattr__(self, "_ReadOnlyQ2Client__client", client)

    def __getattribute__(self, name: str) -> Any:
        if name in {"smembers", "hgetall", "close"}:
            return object.__getattribute__(self, name)
        raise RuntimeError(f"Redis operation/property is not allowed: {name}")

    def smembers(self, key: str) -> Sequence[Any]:
        client = object.__getattribute__(self, "_ReadOnlyQ2Client__client")
        return client.smembers(key)

    def hgetall(self, key: str) -> Mapping[Any, Any]:
        client = object.__getattribute__(self, "_ReadOnlyQ2Client__client")
        return client.hgetall(key)

    def close(self) -> None:
        client = object.__getattribute__(self, "_ReadOnlyQ2Client__client")
        close = getattr(client, "close", None)
        if callable(close):
            close()

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError(f"Redis operation/property is not allowed: {name}")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _aware_now() -> datetime:
    return datetime.now(timezone.utc)


def _rows(snapshot: Any) -> dict[str, dict[str, Any]]:
    return {
        symbol: {
            **quote.to_mapping(),
            "raw_fields": quote.raw_fields,
            "source_time_ms": quote.source_record_time_ms,
        }
        for symbol, quote in snapshot.quotes.items()
    }


def _observe_snapshot(
    *, snapshot: Any, session: AuctionAnchorSessionV1, rows: Mapping[str, Mapping[str, Any]],
    first_read: set[str], wall_started: datetime, wall_completed: datetime,
) -> list[dict[str, Any]]:
    source_times = [
        quote.source_record_time_ms
        for quote in snapshot.quotes.values()
        if quote.source_record_time_ms is not None
    ]
    if not source_times:
        # Pre-open Q2 rows can carry no source time; there is no logical time
        # to evaluate at, so record the observation instead of failing the poll.
        return [{
            "wall_read_started": wall_started.isoformat(),
            "wall_read_completed": wall_completed.isoformat(),
            "source_time_max": None,
            "evaluation_time_ms": None,
            "observed_at_ms": snapshot.envelope.observed_time_ms,
            "projection_status": snapshot.status.value,
            "symbol_count": len(snapshot.expected_symbols),
            "quote_count": len(snapshot.quotes),
            "tag": "ALL",
            "revision": None,
            "available_count": 0,
            "missing_count": 0,
            "late_execution": {"status": "NO_SOURCE_TIMESTAMPS",
                               "timer_signal_id": None, "timer_logical_time_ms": None},
        }]
    source_time_max = max(source_times)
    evaluation_time_ms = source_time_max - source_time_max % 1000
    observed_at_ms = snapshot.envelope.observed_time_ms
    events = []
    for tag, raw_field in ANCHOR_FIELDS.items():
        if tag in first_read:
            outcome = session.observe_late_revision(
                tag, rows, logical_time_ms=evaluation_time_ms,
                observed_at_ms=observed_at_ms, source_layers=(SOURCE_LAYER,),
            )
        else:
            outcome = session.observe_late_start(
                tag, rows, logical_time_ms=evaluation_time_ms,
                anchor_field_observed=any(
                    raw_field in quote.raw_fields for quote in snapshot.quotes.values()
                ),
                observed_at_ms=observed_at_ms, source_layers=(SOURCE_LAYER,),
            )
            first_read.add(tag)
        revision = outcome.revision
        available = len(revision.available_anchor_symbols) if revision else 0
        missing = len(revision.missing_anchor_symbols) if revision else 0
        timer = outcome.timer_signal
        events.append({
            "wall_read_started": wall_started.isoformat(),
            "wall_read_completed": wall_completed.isoformat(),
            "source_time_max": source_time_max,
            "evaluation_time_ms": evaluation_time_ms,
            "observed_at_ms": observed_at_ms,
            "projection_status": snapshot.status.value,
            "symbol_count": len(snapshot.expected_symbols),
            "tag": tag,
            "revision": revision.revision if revision else None,
            "available_count": available,
            "missing_count": missing,
            "late_execution": {
                "status": outcome.status,
                "timer_signal_id": timer.signal_id if timer else None,
                "timer_logical_time_ms": timer.logical_time_ms if timer else None,
            },
        })
    return events


def _write_report(
    out_dir: Path, poll: int, snapshot: Any, session: AuctionAnchorSessionV1,
    pool_artifact: Mapping[str, Any], trade_date: str, wall_completed: datetime,
    data_origin: str,
) -> None:
    times = [q.source_record_time_ms for q in snapshot.quotes.values()
             if q.source_record_time_ms is not None]
    observed = max(times) if times else int(wall_completed.timestamp() * 1000)
    cutoff = observed
    pool = previous_day_limit_pool_from_artifact(
        pool_artifact, trade_date=trade_date, cutoff_ms=cutoff,
        evidence_ref="sealed_previous_day_limit_pool_json",
    )
    report = build_shadow_auction_report(
        trade_date=trade_date, quotes=snapshot.quotes, observation_time_ms=observed,
        recoveries=session.phase_zero_recoveries, previous_pool=pool,
        data_origin=data_origin,
    )
    (out_dir / ("report_%03d.txt" % poll)).write_text(report.text, encoding="utf-8")
    recovered = session.phase_zero_recoveries
    table = {}
    for symbol, quote in snapshot.quotes.items():
        anchor = quote.auction_anchor_0925_price_milli
        source = "a25" if isinstance(anchor, int) and anchor > 0 else None
        if source is None and symbol in recovered:
            anchor, source = recovered[symbol]["price_milli"], "ph0_recovered"
        table[symbol] = {"a25": anchor if source else None, "src": source,
                         "pc": quote.pre_close_milli, "amt": quote.auction_amount_yuan}
    (out_dir / ("anchor_table_%03d.json" % poll)).write_text(
        json.dumps(table, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    with (out_dir / "report_status.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(
            {"poll": poll, "source_time_max": observed, **report.as_mapping()},
            ensure_ascii=False, sort_keys=True) + "\n")


def _capture_client(capture: Mapping[str, Any]) -> ReadOnlyQ2Client:
    reads = capture.get("reads")
    if not isinstance(reads, list):
        raise ValueError("capture reads must be a list")
    sets: dict[str, tuple[Any, ...]] = {}
    hashes: dict[str, Mapping[Any, Any]] = {}
    for item in reads:
        if not isinstance(item, Mapping):
            raise ValueError("capture read item must be an object")
        operation, key, value = item.get("operation"), item.get("key"), item.get("value")
        if not isinstance(key, str):
            raise ValueError("capture read key must be a string")
        if operation == "smembers":
            if not isinstance(value, list) or key in sets:
                raise ValueError("invalid or duplicate captured SMEMBERS result")
            sets[key] = tuple(value)
        elif operation == "hgetall":
            if not isinstance(value, Mapping) or key in hashes:
                raise ValueError("invalid or duplicate captured HGETALL result")
            hashes[key] = dict(value)

    class CaptureClient:
        def smembers(self, key: str) -> Sequence[Any]:
            return sets.get(key, ())

        def hgetall(self, key: str) -> Mapping[Any, Any]:
            return hashes.get(key, {})

    return ReadOnlyQ2Client(CaptureClient())


def run_shadow(
    *, source: str, out_dir: Path, capture_path: Path | None = None,
    live_client: Any | None = None, trade_date: str | None = None,
    poll_seconds: float = 1.0, max_polls: int = 1,
    confirm_read_only: bool = False,
    recover_final_from_phase_zero: bool = False,
    pool_path: Path | None = None,
) -> dict[str, Any]:
    if source == "live" and not confirm_read_only:
        raise RuntimeError("live source requires confirm_read_only=True")
    if source != "live":
        return _run_shadow_impl(
            source=source, out_dir=out_dir, capture_path=capture_path,
            live_client=live_client, trade_date=trade_date,
            poll_seconds=poll_seconds, max_polls=max_polls,
            confirm_read_only=confirm_read_only,
            recover_final_from_phase_zero=recover_final_from_phase_zero,
            pool_path=pool_path,
        )
    try:
        return _run_shadow_impl(
            source=source, out_dir=out_dir, capture_path=capture_path,
            live_client=live_client, trade_date=trade_date,
            poll_seconds=poll_seconds, max_polls=max_polls,
            confirm_read_only=confirm_read_only,
            recover_final_from_phase_zero=recover_final_from_phase_zero,
            pool_path=pool_path,
        )
    except Exception as exc:
        raise RuntimeError(f"live shadow failed: {type(exc).__name__}") from None


def _record_failure(out_dir: Path, exc: BaseException) -> None:
    """Keep the cause for diagnosis; Core adapters never see the password."""
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "failure.json").write_text(
            json.dumps({"type": type(exc).__name__, "message": str(exc)[:300]}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass


def _run_shadow_impl(
    *, source: str, out_dir: Path, capture_path: Path | None = None,
    live_client: Any | None = None, trade_date: str | None = None,
    poll_seconds: float = 1.0, max_polls: int = 1,
    confirm_read_only: bool = False,
    recover_final_from_phase_zero: bool = False,
    pool_path: Path | None = None,
) -> dict[str, Any]:
    if out_dir.exists():
        raise FileExistsError(f"output directory already exists: {out_dir}")
    capture_sha256 = None
    if source == "capture":
        if capture_path is None:
            raise ValueError("capture source requires --capture")
        capture_bytes = capture_path.read_bytes()
        capture_sha256 = _sha256(capture_bytes)
        capture = json.loads(capture_bytes)
        if capture.get("contract_version") != "RedisQ2ReadCaptureV1":
            raise ValueError("unsupported sealed Redis capture contract")
        trade_date = str(capture.get("trade_date", ""))
        client = _capture_client(capture)
    elif source == "live":
        if live_client is None:
            raise ValueError("live source requires a Redis client")
        trade_date = str(trade_date or "")
        client = ReadOnlyQ2Client(live_client)
    else:
        raise ValueError("source must be capture or live")
    if not trade_date:
        raise ValueError("trade date is required for Q2 projection")

    pool_artifact = None
    if pool_path is not None:
        pool_artifact = json.loads(pool_path.read_bytes())
    out_dir.mkdir(parents=True, exist_ok=False)
    events_path = out_dir / "shadow_events.jsonl"
    session = None
    first_read: set[str] = set()
    try:
        with events_path.open("w", encoding="utf-8") as events_file:
            for poll in range(max_polls):
                wall_started = _aware_now()
                snapshot = RedisQ2ProjectionAdapter(client).read(
                    trade_date, freshness_policy=FreshnessPolicy()
                )
                wall_completed = _aware_now()
                if session is None:
                    session = AuctionAnchorSessionV1(
                        trade_date, snapshot.expected_symbols,
                        signal_prefix="redis-q2-shadow",
                        recover_final_from_phase_zero=recover_final_from_phase_zero,
                    )
                rows = _rows(snapshot)
                for event in _observe_snapshot(
                    snapshot=snapshot, session=session, rows=rows,
                    first_read=first_read, wall_started=wall_started,
                    wall_completed=wall_completed,
                ):
                    if recover_final_from_phase_zero:
                        event["phase_zero_recovered_count"] = len(
                            session.phase_zero_recoveries
                        )
                    events_file.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
                events_file.flush()
                if pool_artifact is not None:
                    _write_report(
                        out_dir, poll, snapshot, session, pool_artifact, trade_date,
                        wall_completed,
                        "LIVE_REDIS_READ_ONLY" if source == "live" else "SEALED_REDIS_CAPTURE",
                    )
                if poll + 1 < max_polls:
                    time.sleep(poll_seconds)
    except Exception as exc:
        _record_failure(out_dir, exc)
        raise
    finally:
        client.close()

    manifest = {
        "contract": "Task008RedisAuctionSessionShadowV1",
        "source": source,
        "trade_date": trade_date,
        "capture_sha256": capture_sha256 if source == "capture" else "未记录凭据",
        "read_only": True,
        "redis_network_access": "NONE_SEALED_CAPTURE_ONLY" if source == "capture" else "LIVE_READS_ONLY",
        "poll_seconds": poll_seconds,
        "max_polls": max_polls,
        "event_count": max_polls * len(ANCHOR_FIELDS),
        **(
            {
                "recover_final_from_phase_zero": True,
                "phase_zero_recovered_count": len(session.phase_zero_recoveries)
                if session is not None
                else 0,
            }
            if recover_final_from_phase_zero
            else {}
        ),
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=("capture", "live"), required=True)
    parser.add_argument("--capture", type=Path)
    parser.add_argument("--trade-date", help="required for live source (YYYY-MM-DD)")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--max-polls", type=int, default=1)
    parser.add_argument("--confirm-read-only", action="store_true")
    parser.add_argument("--recover-final-from-phase-zero", action="store_true",
                        help="opt-in: use a late ph==0 final-match snapshot as the 0925 anchor when a25 is missing")
    parser.add_argument("--pool", type=Path,
                        help="sealed previous-day limit pool JSON; enables a report_NNN.txt per poll")
    args = parser.parse_args()
    if args.poll_seconds < 0 or args.max_polls < 1:
        parser.error("poll-seconds must be non-negative and max-polls must be positive")
    live_client = None
    if args.source == "live":
        if not args.confirm_read_only:
            parser.error("live source requires --confirm-read-only")
        if not args.trade_date:
            parser.error("live source requires --trade-date")
        try:
            port = int(os.environ.get("REDIS_PORT", "6379"))
            db = int(os.environ.get("REDIS_DB", "0"))
            import redis  # type: ignore[import-not-found]

            live_client = redis.Redis(
                host=os.environ.get("REDIS_HOST", "127.0.0.1"),
                port=port,
                db=db,
                password=os.environ.get("REDIS_PASSWORD"),
                decode_responses=True,
                socket_timeout=5,
                socket_connect_timeout=5,
            )
            run_shadow(
                source=args.source, capture_path=args.capture, out_dir=args.out_dir,
                live_client=live_client, trade_date=args.trade_date,
                poll_seconds=args.poll_seconds,
                max_polls=args.max_polls,
                confirm_read_only=args.confirm_read_only,
                recover_final_from_phase_zero=args.recover_final_from_phase_zero,
                pool_path=args.pool,
            )
        except Exception as exc:
            raise RuntimeError(f"live shadow failed: {type(exc).__name__}") from None
        return 0
    run_shadow(
        source=args.source, capture_path=args.capture, out_dir=args.out_dir,
        live_client=live_client, trade_date=args.trade_date,
        poll_seconds=args.poll_seconds,
        max_polls=args.max_polls,
        confirm_read_only=args.confirm_read_only,
        recover_final_from_phase_zero=args.recover_final_from_phase_zero,
        pool_path=args.pool,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
