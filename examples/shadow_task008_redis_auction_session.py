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
from engine_core.live_input_adapters import previous_day_limit_pool_from_state  # noqa: E402
from engine_core.live_inputs import LiveInputCache, LiveInputReader  # noqa: E402
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


class _RecordingClient:
    """Record the Q2 projection reads so a poll can be sealed as a capture."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self.reads: list[dict[str, Any]] = []

    def smembers(self, key: str) -> Sequence[Any]:
        value = self._client.smembers(key)
        self.reads.append({"operation": "smembers", "key": key,
                           "value": sorted(str(item) for item in value)})
        return value

    def hgetall(self, key: str) -> Mapping[Any, Any]:
        value = self._client.hgetall(key)
        self.reads.append({"operation": "hgetall", "key": key,
                           "value": {str(k): str(v) for k, v in dict(value).items()}})
        return value

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()

    def drain(self) -> list[dict[str, Any]]:
        reads, self.reads = self.reads, []
        return reads


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
    observed_at_ms = snapshot.envelope.observed_time_ms
    # Q2 source stamps can stall (live 2026-10-13: nothing newer than 09:25:03
    # until 09:26:00), so the barrier clock is the later of source time and the
    # read time; evaluating at source time alone never opens the 0925 barrier.
    clock_ms = max(source_time_max, observed_at_ms)
    evaluation_time_ms = clock_ms - clock_ms % 1000
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
            if outcome.status == "LATE_START_OBSERVED":
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


def _write_tag_summaries(out_dir: Path, poll: int, snapshot: Any, trade_date: str) -> None:
    """Per-tag (0920/0924/0925) Q2 summary metrics, comparable with the legacy
    ``market:auction:{date}:{tag}`` summaries when read at their timestamps."""

    from engine_core.market_summary import derive_q2_auction_summary

    times = [q.source_record_time_ms for q in snapshot.quotes.values()
             if q.source_record_time_ms is not None]
    if not times:
        return
    observed = max(times)
    record: dict[str, Any] = {"poll": poll, "source_time_max": observed}
    for tag in ("0920", "0924", "0925"):
        summary = derive_q2_auction_summary(
            snapshot.quotes, trade_date=trade_date, source_id="redis_q2_shadow",
            source_table="Q2:redis_projection", observation_time_ms=observed,
            anchor_tag=tag,
        )
        record[tag] = {"status": summary.status.value, **summary.metrics}
    with (out_dir / "tag_summaries.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _write_td_reconstruction(out_dir: Path, trade_date: str, td_rows_path: Path) -> None:
    """Rebuild the missed auction facts from sealed TD rows (``td_reconstruction.json``)."""

    from engine_core import reconstruct_auction_from_td

    rows = [json.loads(line) for line in td_rows_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    trace = dict(reconstruct_auction_from_td(trade_date, rows).as_trace())
    trace["td_rows_sha256"] = _sha256(td_rows_path.read_bytes())
    (out_dir / "td_reconstruction.json").write_text(
        json.dumps(trace, ensure_ascii=False, sort_keys=True, indent=1, default=str) + "\n",
        encoding="utf-8",
    )


def _write_cold_start(out_dir: Path, trade_date: str, start_ms: int) -> dict:
    """Record what this session start can and cannot rebuild (``cold_start.json``)."""

    from engine_core import (
        assess_cold_start, build_a_share_session_plan, build_calendar_snapshot,
    )

    calendar = build_calendar_snapshot(
        [trade_date], version="shadow-one-day", declared_valid_from=trade_date,
        declared_valid_to=trade_date, source_guard_valid_from=trade_date,
        source_guard_valid_to=trade_date, source_id="explicit_shadow_trade_date",
        observed_at_ms=start_ms, evidence_ref="shadow://explicit-trade-date",
    )
    plan = build_a_share_session_plan(trade_date, calendar)
    try:
        record = dict(assess_cold_start(plan, start_ms).as_mapping())
    except ValueError:
        # The read happened on another local date than the requested session
        # (e.g. a post-hoc read of an earlier day): no phase can be assigned.
        record = {"contract": "ColdStartAssessmentV1", "trade_date": trade_date,
                  "start_ms": start_ms, "status": "START_DATE_MISMATCH"}
    (out_dir / "cold_start.json").write_text(
        json.dumps(record, ensure_ascii=False, sort_keys=True, indent=1) + "\n",
        encoding="utf-8",
    )
    return record


def _report_observed_ms(snapshot: Any, wall_completed: datetime) -> int:
    times = [q.source_record_time_ms for q in snapshot.quotes.values()
             if q.source_record_time_ms is not None]
    return max(times) if times else int(wall_completed.timestamp() * 1000)


def _write_report(
    out_dir: Path, poll: int, snapshot: Any, session: AuctionAnchorSessionV1,
    pool: Any, trade_date: str, wall_completed: datetime,
    data_origin: str,
) -> None:
    times = [q.source_record_time_ms for q in snapshot.quotes.values()
             if q.source_record_time_ms is not None]
    observed = max(times) if times else int(wall_completed.timestamp() * 1000)
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
                         "pc": quote.pre_close_milli, "amt": quote.auction_amount_yuan,
                         "px": getattr(quote, "price_milli", None)}
        if source == "ph0_recovered":
            # evidence for the amount ~ price * lots * 100 / 1000 consistency check
            table[symbol]["rec_lots"] = recovered[symbol].get("volume_lots")
            table[symbol]["rec_amt"] = recovered[symbol].get("amount_yuan")
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
    read_times: Sequence[str] | None = None,
    live_pool_previous_date: str | None = None,
    seal_captures: bool = False,
    td_rows_path: Path | None = None,
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
            pool_path=pool_path, read_times=read_times,
            live_pool_previous_date=live_pool_previous_date,
            seal_captures=seal_captures,
            td_rows_path=td_rows_path,
        )
    try:
        return _run_shadow_impl(
            source=source, out_dir=out_dir, capture_path=capture_path,
            live_client=live_client, trade_date=trade_date,
            poll_seconds=poll_seconds, max_polls=max_polls,
            confirm_read_only=confirm_read_only,
            recover_final_from_phase_zero=recover_final_from_phase_zero,
            pool_path=pool_path, read_times=read_times,
            live_pool_previous_date=live_pool_previous_date,
            seal_captures=seal_captures,
            td_rows_path=td_rows_path,
        )
    except Exception as exc:
        raise RuntimeError(f"live shadow failed: {type(exc).__name__}") from None


def _sleep_until_local(trade_date: str, hms: str) -> None:
    """Sleep until ``trade_date`` ``HH:MM:SS`` Asia/Shanghai; return at once if past."""
    from zoneinfo import ZoneInfo

    target = datetime.fromisoformat(f"{trade_date}T{hms}").replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    delay = (target - datetime.now(timezone.utc)).total_seconds()
    if delay > 0:
        time.sleep(delay)


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
    read_times: Sequence[str] | None = None,
    live_pool_previous_date: str | None = None,
    seal_captures: bool = False,
    td_rows_path: Path | None = None,
) -> dict[str, Any]:
    if out_dir.exists():
        raise FileExistsError(f"output directory already exists: {out_dir}")
    capture_sha256 = None
    recorder = None
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
        recorder = _RecordingClient(live_client) if seal_captures else None
        client = ReadOnlyQ2Client(recorder or live_client)
    else:
        raise ValueError("source must be capture or live")
    if not trade_date:
        raise ValueError("trade date is required for Q2 projection")

    pool_artifact = None
    if pool_path is not None:
        pool_artifact = json.loads(pool_path.read_bytes())
    pool_reader = pool_cache = None
    if live_pool_previous_date is not None:
        if source != "live" or pool_artifact is not None:
            raise ValueError("--live-pool-previous-date needs live source and no --pool")
        # Raw client on purpose: the Q2 wrapper only allows projection reads;
        # the layer itself only calls type/hlen/hscan_iter/get/ttl.
        pool_reader = LiveInputReader(live_client, now_ms=lambda: int(time.time() * 1000))
        pool_cache = LiveInputCache()
    out_dir.mkdir(parents=True, exist_ok=False)
    events_path = out_dir / "shadow_events.jsonl"
    session = None
    first_read: set[str] = set()
    try:
        with events_path.open("w", encoding="utf-8") as events_file:
            if read_times:
                max_polls = len(read_times)
            for poll in range(max_polls):
                if read_times and source == "live":
                    _sleep_until_local(trade_date, read_times[poll])
                wall_started = _aware_now()
                snapshot = RedisQ2ProjectionAdapter(client).read(
                    trade_date, freshness_policy=FreshnessPolicy()
                )
                wall_completed = _aware_now()
                if recorder is not None:
                    sealed = {
                        "contract_version": "RedisQ2ReadCaptureV1",
                        "trade_date": trade_date,
                        "read_completed_at": wall_completed.isoformat(),
                        "reads": recorder.drain(),
                    }
                    with (out_dir / ("capture_%03d.json" % poll)).open("x", encoding="utf-8") as sealed_file:
                        json.dump(sealed, sealed_file, ensure_ascii=False, sort_keys=True,
                                  separators=(",", ":"))
                if session is None and source == "live":
                    record = _write_cold_start(out_dir, trade_date, snapshot.envelope.observed_time_ms)
                    needs_td = "TD_RECONSTRUCTION_REQUIRED" in (
                        record.get("theme_amount_delta_0924_0925"), record.get("opening_0932"))
                    if td_rows_path is not None and needs_td:
                        _write_td_reconstruction(out_dir, trade_date, td_rows_path)
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
                _write_tag_summaries(out_dir, poll, snapshot, trade_date)
                pool = None
                if pool_artifact is not None:
                    pool = previous_day_limit_pool_from_artifact(
                        pool_artifact, trade_date=trade_date,
                        cutoff_ms=_report_observed_ms(snapshot, wall_completed),
                        evidence_ref="sealed_previous_day_limit_pool_json",
                    )
                elif pool_reader is not None:
                    state = pool_cache.refresh(pool_reader.read(
                        "yest_limit_pool", trade_date=trade_date,
                        previous_trade_date=live_pool_previous_date,
                    ))
                    with (out_dir / "live_inputs.jsonl").open("a", encoding="utf-8") as trace:
                        trace.write(json.dumps({"poll": poll, **state.as_trace()}, sort_keys=True) + "\n")
                    pool = previous_day_limit_pool_from_state(
                        state, trade_date=trade_date,
                        previous_trade_date=live_pool_previous_date,
                        cutoff_ms=_report_observed_ms(snapshot, wall_completed),
                    )
                if pool is not None:
                    _write_report(
                        out_dir, poll, snapshot, session, pool, trade_date,
                        wall_completed,
                        "LIVE_REDIS_READ_ONLY" if source == "live" else "SEALED_REDIS_CAPTURE",
                    )
                if poll + 1 < max_polls and not read_times:
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


def _parse_times(text: str | None) -> list[str] | None:
    if not text:
        return None
    times = [item.strip() for item in text.split(",") if item.strip()]
    for item in times:
        datetime.strptime(item, "%H:%M:%S")
    return times


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
    parser.add_argument("--read-times",
                        help="live only: comma-separated local HH:MM:SS read times in one session "
                        "(replaces --max-polls/--poll-seconds)")
    parser.add_argument("--td-rows", type=Path,
                        help="sealed auction_snapshot_v2 JSONL; when the cold start needs TD "
                        "reconstruction, write td_reconstruction.json from it")
    parser.add_argument("--seal-captures", action="store_true",
                        help="live only: write each poll's Q2 reads as capture_NNN.json "
                        "(RedisQ2ReadCaptureV1) so the poll can be replayed with --source capture")
    parser.add_argument("--live-pool-previous-date",
                        help="read the previous-day limit pool live (YYYY-MM-DD of the previous session) "
                        "through the live input layer instead of --pool")
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
                read_times=_parse_times(args.read_times),
                live_pool_previous_date=args.live_pool_previous_date,
                seal_captures=args.seal_captures,
                td_rows_path=args.td_rows,
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
        read_times=_parse_times(args.read_times),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
