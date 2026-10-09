"""Single read-only Q2 observation; not a historical cutoff reconstruction."""

import argparse
import hashlib
import json
import os
import platform
import sys
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from engine_core import (
    DeterministicEngine, EngineSignal, FreshnessPolicy, MarketStateReducer,
    ProbeStrategy, RedisQ2ProjectionAdapter, SignalKind, WindowManager, WindowSpec,
)

REPORT_CONTRACT = "LiveQ2CoreProbeV2"
READ_CAPTURE_CONTRACT = "RedisQ2ReadCaptureV1"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _date_text(value):
    parsed = datetime.strptime(value, "%Y-%m-%d").date()
    if parsed.isoformat() != value:
        raise ValueError("trade date must be strict YYYY-MM-DD")
    return value


def _revision_metadata():
    """Identify the code that produced the evidence without exposing env data."""

    def git_text(*args):
        try:
            result = subprocess.run(
                ("git",) + args,
                cwd=REPOSITORY_ROOT,
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            return "UNKNOWN"
        return result.stdout.strip() if result.returncode == 0 else "UNKNOWN"

    return {
        "core_commit": git_text("rev-parse", "HEAD"),
        "branch": git_text("branch", "--show-current"),
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "python_version": platform.python_version(),
        "python_executable": sys.executable,
    }


class ReadOnlyCapture:
    """Expose only adapter read operations and retain the exact returned input."""

    def __init__(self, client):
        self.client = client
        self.reads = []

    def smembers(self, key):
        value = sorted(self.client.smembers(key))
        self.reads.append({"operation": "smembers", "key": key, "value": value})
        return value

    def hgetall(self, key):
        value = dict(self.client.hgetall(key))
        self.reads.append({"operation": "hgetall", "key": key, "value": value})
        return value


def _canonical_read_bytes(reads):
    return json.dumps(
        reads,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def build_q2_read_capture_artifact(trade_date, report, reads):
    """Build a credential-free, hash-pinned transcript of actual Redis reads."""

    if _date_text(trade_date) != report.get("trade_date"):
        raise ValueError("capture trade_date does not match report")
    if any(
        not isinstance(item, dict)
        or item.get("operation") not in {"smembers", "hgetall"}
        or not isinstance(item.get("key"), str)
        or (
            item.get("operation") == "smembers"
            and not isinstance(item.get("value"), list)
        )
        or (
            item.get("operation") == "hgetall"
            and not isinstance(item.get("value"), dict)
        )
        for item in reads
    ):
        raise ValueError("capture supports read-only SMEMBERS/HGETALL operations only")
    reads_hash = hashlib.sha256(_canonical_read_bytes(reads)).hexdigest()
    if reads_hash != report.get("input_canonical_sha256"):
        raise ValueError("capture reads do not match report input hash")
    return {
        "contract_version": READ_CAPTURE_CONTRACT,
        "trade_date": trade_date,
        "read_started_at": report.get("read_started_at"),
        "read_completed_at": report.get("read_completed_at"),
        "observation_time_mode": report.get("observation_time_mode"),
        "freshness_policy_stale_after_ms": report.get(
            "freshness_policy_stale_after_ms"
        ),
        "volume_unit_diagnostic_symbols": report.get(
            "volume_unit_diagnostic_symbols", []
        ),
        "operations": ["smembers", "hgetall"],
        "input_canonical_sha256": reads_hash,
        "reads": reads,
        "limitations": [
            "best_effort_non_atomic_redis_read",
            "source_record_time_is_not_redis_available_at",
            "active_membership_does_not_prove_full_market_coverage",
        ],
    }


class CapturedQ2ReadClient:
    """Replay a saved Redis read transcript through the existing Q2 adapter."""

    def __init__(self, artifact):
        if not isinstance(artifact, dict) or artifact.get("contract_version") != READ_CAPTURE_CONTRACT:
            raise ValueError("unsupported Q2 read capture contract")
        _date_text(artifact.get("trade_date", ""))
        reads = artifact.get("reads")
        if not isinstance(reads, list):
            raise ValueError("capture reads must be a list")
        if any(
            not isinstance(item, dict)
            or item.get("operation") not in {"smembers", "hgetall"}
            or not isinstance(item.get("key"), str)
            or (
                item.get("operation") == "smembers"
                and not isinstance(item.get("value"), list)
            )
            or (
                item.get("operation") == "hgetall"
                and not isinstance(item.get("value"), dict)
            )
            for item in reads
        ):
            raise ValueError("capture supports read-only SMEMBERS/HGETALL operations only")
        reads_hash = hashlib.sha256(_canonical_read_bytes(reads)).hexdigest()
        if reads_hash != artifact.get("input_canonical_sha256"):
            raise ValueError("capture input hash mismatch")
        completed_at = artifact.get("read_completed_at")
        try:
            parsed_completed_at = datetime.fromisoformat(completed_at)
        except (TypeError, ValueError) as exc:
            raise ValueError("capture read_completed_at must be ISO datetime") from exc
        if parsed_completed_at.tzinfo is None or parsed_completed_at.utcoffset() is None:
            raise ValueError("capture read_completed_at must include a timezone")
        self.trade_date = artifact["trade_date"]
        self.read_completed_at = parsed_completed_at
        self._reads = reads
        self._index = 0

    def _next(self, operation, key):
        if self._index >= len(self._reads):
            raise ValueError("capture ended before requested Redis read")
        item = self._reads[self._index]
        if item["operation"] != operation or item["key"] != key:
            raise ValueError("capture read order/key mismatch")
        self._index += 1
        return item["value"]

    def smembers(self, key):
        value = self._next("smembers", key)
        if not isinstance(value, list):
            raise ValueError("captured SMEMBERS value must be a list")
        return value

    def hgetall(self, key):
        value = self._next("hgetall", key)
        if not isinstance(value, dict):
            raise ValueError("captured HGETALL value must be an object")
        return value

    def assert_consumed(self):
        if self._index != len(self._reads):
            raise ValueError("capture contains unused Redis reads")


def run_engine(projection, now):
    engine = DeterministicEngine(
        MarketStateReducer(), WindowManager((WindowSpec("observation", now, now + 1),)),
        ProbeStrategy(), session_id=projection.trade_date, phase="READ_ONLY_PROBE",
    )
    engine.submit(EngineSignal("observation", now, 1, SignalKind.MARKET_UPDATE, projection))
    engine.submit(EngineSignal("probe", now + 1, 2, SignalKind.TIMER,
                               {"trigger_id": "READ_ONLY_PROBE", "close_windows": ("observation",)}))
    result = engine.run_until_empty()
    return {"processed_signals": result.processed_signals,
            "snapshot_hashes": [item.content_hash for item in result.snapshots],
            "probe_hashes": [item.content_hash for item in result.strategy_results]}


def build_volume_unit_diagnostics(reads, symbols):
    """Expose dimensional evidence without promoting either unit to truth."""

    requested = {str(symbol).strip()[-6:] for symbol in symbols if str(symbol).strip()}
    diagnostics = []
    for item in reads:
        key = str(item.get("key", ""))
        if item.get("operation") != "hgetall" or not key.startswith("q2:"):
            continue
        symbol = key.rsplit(":", 1)[-1]
        if requested and symbol not in requested:
            continue
        row = item.get("value") or {}
        try:
            price_yuan = int(row["px"]) / 1000.0
            amount_yuan = int(row["amt"])
            raw_volume = int(row["vol"])
        except (KeyError, TypeError, ValueError):
            continue
        if price_yuan <= 0 or amount_yuan < 0 or raw_volume <= 0:
            continue
        implied_lots = amount_yuan / (raw_volume * 100.0)
        implied_shares = amount_yuan / raw_volume
        diagnostics.append({
            "symbol": symbol,
            "source_record_time_ms": int(row["ts"]) if str(row.get("ts", "")).isdigit() else None,
            "current_price_yuan": price_yuan,
            "cumulative_amount_yuan": amount_yuan,
            "raw_volume": raw_volume,
            "implied_average_price_yuan_if_lots": implied_lots,
            "implied_average_price_yuan_if_shares": implied_shares,
            "lots_to_current_price_ratio": implied_lots / price_yuan,
            "shares_to_current_price_ratio": implied_shares / price_yuan,
        })
    return sorted(diagnostics, key=lambda item: item["symbol"])


def observe(
    client,
    trade_date,
    observed_at=None,
    stale_after_ms=None,
    diagnostic_symbols=(),
    *,
    include_raw_capture=False,
):
    capture = ReadOnlyCapture(client)
    if observed_at is None:
        started_at = datetime.now(timezone.utc)
        # Q2ProjectionSnapshot stores observation time in integer milliseconds.
        # Use the same precision for the start boundary so a same-millisecond
        # read cannot appear to complete before it started after serialization.
        read_started_at = started_at.replace(
            microsecond=(started_at.microsecond // 1000) * 1000
        )
    else:
        read_started_at = None
    projection = RedisQ2ProjectionAdapter(capture).read(
        trade_date, observed_at, freshness_policy=FreshnessPolicy(stale_after_ms=stale_after_ms))
    observed_ms = projection.envelope.observed_time_ms
    effective_observed_at = datetime.fromtimestamp(observed_ms / 1000.0, timezone.utc)
    now = observed_ms
    first, second = run_engine(projection, now), run_engine(projection, now)
    input_bytes = json.dumps(capture.reads, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
    read_counts = Counter(item["operation"] for item in capture.reads)
    raw_hashes = [item["value"] for item in capture.reads if item["operation"] == "hgetall"]
    tracked_fields = ("ts", "px", "pc", "amt", "vol", "amt2m", "amt5m", "a20", "a24", "a25",
                      "am", "br", "ar", "ls", "ph", "mk")
    field_presence = {field: sum(field in row and row.get(field) not in (None, "") for row in raw_hashes)
                      for field in tracked_fields}
    field_explicit_zero = {field: sum(str(row.get(field)) == "0" for row in raw_hashes if field in row)
                           for field in tracked_fields}
    market_counts = dict(sorted(Counter(str(row.get("mk", "")) for row in raw_hashes).items()))
    phase_counts = dict(sorted(Counter(str(row.get("ph", "")) for row in raw_hashes).items()))
    value_counts = {
        field: dict(sorted(Counter(str(row.get(field, "")) for row in raw_hashes).items()))
        for field in ("ls", "ph", "mk")
    }
    error_counts = Counter(error for errors in (q.field_errors for q in projection.quotes.values())
                           for error in errors)
    source_ages_ms = sorted(
        observed_ms - quote.source_record_time_ms
        for quote in projection.quotes.values()
        if quote.source_record_time_ms is not None
    )
    newest_age_seconds = (
        (observed_ms - projection.newest_source_time_ms) / 1000.0
        if projection.newest_source_time_ms is not None
        else None
    )
    age_summary = {
        "count": len(source_ages_ms),
        "minimum_ms": source_ages_ms[0] if source_ages_ms else None,
        "median_ms": source_ages_ms[len(source_ages_ms) // 2] if source_ages_ms else None,
        "maximum_ms": source_ages_ms[-1] if source_ages_ms else None,
        "future_source_time_count": sum(age < 0 for age in source_ages_ms),
    }
    result = {
        "report_contract": REPORT_CONTRACT,
        "trade_date": trade_date,
        "read_started_at": read_started_at.isoformat() if read_started_at else None,
        "read_completed_at": effective_observed_at.isoformat(),
        "observed_at": effective_observed_at.isoformat(),
        "observation_time_mode": "LIVE_READ_COMPLETION" if read_started_at else "FIXED_INPUT_TIME",
        "freshness_policy_stale_after_ms": stale_after_ms,
        "status": projection.status.value, "consistency": projection.consistency_status,
        "status_scope": (
            "Projection status is evaluated against the q2:active membership captured by this read "
            "and the configured validation/freshness policy; it does not prove full-market coverage."
        ),
        "requested_count": len(projection.expected_symbols), "quote_count": len(projection.quotes),
        "missing_symbol_count": len(projection.missing_symbols),
        "missing_symbol_samples": list(projection.missing_symbols[:20]),
        "stale_symbol_count": len(projection.stale_symbols),
        "stale_symbol_samples": list(projection.stale_symbols[:20]),
        "field_error_counts": dict(sorted(error_counts.items())),
        "field_error_samples": dict(list(
            (s, list(q.field_errors)) for s, q in projection.quotes.items() if q.field_errors
        )[:20]),
        "row_coverage": projection.coverage, "universe_authority": "NOT_PROVEN_BY_ACTIVE_SET",
        "source_time_semantics": (
            "source_record_time_ms is the Q2 ts field; it is not Redis available_at, "
            "Rabbit arrival time, or proof of historical visibility."
        ),
        "oldest_source_time_ms": projection.oldest_source_time_ms,
        "newest_source_time_ms": projection.newest_source_time_ms,
        "source_record_age_ms": age_summary,
        "newest_source_record_age_seconds": newest_age_seconds,
        "projection_hash": projection.content_hash,
        "engine_run1": first, "engine_run2": second,
        "same_observation_engine_deterministic": first == second,
        "input_canonical_sha256": hashlib.sha256(input_bytes).hexdigest(),
        "read_operation_counts": dict(sorted(read_counts.items())),
        "read_only_key_count": len(capture.reads),
        "raw_field_presence_counts": field_presence,
        "raw_field_explicit_zero_counts": field_explicit_zero,
        "raw_market_counts": market_counts,
        "raw_phase_counts": phase_counts,
        "raw_value_counts": value_counts,
        "volume_unit_diagnostics": build_volume_unit_diagnostics(
            capture.reads, diagnostic_symbols),
        "volume_unit_diagnostic_symbols": list(diagnostic_symbols),
        "core_validation_scope": (
            "Q2 projection ingestion and same-input ProbeStrategy repeatability only; "
            "not a business strategy, tick-batch, Rabbit, or production-equivalence test."
        ),
        "limitations": ["non-atomic Redis observation",
                        "volume diagnostics do not infer the separate iv field contract",
                        "q2:active membership does not prove authoritative full-market coverage",
                        "not historical replay or live deployment acceptance"],
        "side_effect_proof": "only smembers/hgetall exposed; TD/claim/notification/SMTP not assembled",
    }
    if include_raw_capture:
        result["_raw_read_capture"] = capture.reads
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument(
        "--stale-after-ms", type=int, default=None,
        help="optional source-time age classification; omitted means freshness is descriptive only",
    )
    parser.add_argument("--output", type=Path, required=True)
    capture_group = parser.add_mutually_exclusive_group()
    capture_group.add_argument(
        "--capture-input",
        type=Path,
        help="optionally save exact read-only Redis results to a separate validation artifact",
    )
    capture_group.add_argument(
        "--replay-input",
        type=Path,
        help="replay a RedisQ2ReadCaptureV1 artifact without connecting to Redis",
    )
    parser.add_argument(
        "--diagnostic-symbols",
        default=None,
        help="bounded comma-separated symbols for audit-only volume dimensional evidence",
    )
    args = parser.parse_args()
    trade_date = _date_text(args.trade_date)
    if args.stale_after_ms is not None and args.stale_after_ms < 0:
        parser.error("stale-after-ms must be nonnegative")
    if args.capture_input and args.capture_input.resolve() == args.output.resolve():
        parser.error("capture-input and output paths must differ")
    if args.output.exists() or args.output.with_name(args.output.name + ".sha256").exists():
        parser.error("output and checksum paths must not already exist")
    if args.capture_input and args.capture_input.exists():
        parser.error("capture-input path must not already exist")

    if args.replay_input:
        try:
            artifact = json.loads(args.replay_input.read_text(encoding="utf-8"))
            client = CapturedQ2ReadClient(artifact)
            if client.trade_date != trade_date:
                raise ValueError("capture trade_date does not match requested trade_date")
            diagnostic_symbols = (
                tuple(
                    symbol.strip()
                    for symbol in args.diagnostic_symbols.split(",")
                    if symbol.strip()
                )
                if args.diagnostic_symbols is not None
                else tuple(artifact.get("volume_unit_diagnostic_symbols", ()))
            )
            stale_after_ms = (
                args.stale_after_ms
                if args.stale_after_ms is not None
                else artifact.get("freshness_policy_stale_after_ms")
            )
            result = observe(
                client,
                trade_date,
                client.read_completed_at,
                stale_after_ms,
                diagnostic_symbols,
            )
            client.assert_consumed()
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            parser.error(f"cannot replay Q2 read capture: {exc}")
        result["observation_time_mode"] = "FIXED_CAPTURED_READ_COMPLETION"
        result["captured_source_observation"] = {
            "read_started_at": artifact.get("read_started_at"),
            "read_completed_at": artifact["read_completed_at"],
            "capture_input_sha256": artifact["input_canonical_sha256"],
            "capture_file_sha256": hashlib.sha256(args.replay_input.read_bytes()).hexdigest(),
            "capture_contract_version": READ_CAPTURE_CONTRACT,
            "freshness_policy_stale_after_ms": artifact.get(
                "freshness_policy_stale_after_ms"
            ),
            "freshness_policy_overridden": (
                args.stale_after_ms is not None
                and args.stale_after_ms
                != artifact.get("freshness_policy_stale_after_ms")
            ),
        }
    else:
        diagnostic_symbols = (
            tuple(
                symbol.strip()
                for symbol in args.diagnostic_symbols.split(",")
                if symbol.strip()
            )
            if args.diagnostic_symbols is not None
            else ("000001", "300750", "600519")
        )
        import redis  # Linux runtime dependency only; no connection during imports/tests.
        client = redis.Redis(host=os.environ.get("REDIS_HOST", "127.0.0.1"),
                             port=int(os.environ.get("REDIS_PORT", "6379")),
                             db=int(os.environ.get("REDIS_DB", "0")),
                             password=os.environ.get("REDIS_PASSWORD"),
                             decode_responses=True, socket_timeout=5, socket_connect_timeout=5)
        try:
            result = observe(
                client,
                trade_date,
                None,
                args.stale_after_ms,
                diagnostic_symbols,
                include_raw_capture=args.capture_input is not None,
            )
        finally:
            client.close()
        if args.capture_input:
            reads = result.pop("_raw_read_capture")
            artifact = build_q2_read_capture_artifact(trade_date, result, reads)
            capture_content = (
                json.dumps(artifact, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
            )
            args.capture_input.parent.mkdir(parents=True, exist_ok=True)
            with args.capture_input.open("x", encoding="utf-8") as capture_output:
                capture_output.write(capture_content)
                capture_output.flush()
                os.fsync(capture_output.fileno())
            result["input_capture"] = {
                "path": str(args.capture_input),
                "contract_version": READ_CAPTURE_CONTRACT,
                "file_sha256": hashlib.sha256(capture_content.encode("utf-8")).hexdigest(),
                "input_canonical_sha256": artifact["input_canonical_sha256"],
                "read_operation_count": len(reads),
            }

    result["runtime"] = _revision_metadata()
    result["report_generated_at"] = datetime.now(timezone.utc).isoformat()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    with args.output.open("x", encoding="utf-8") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())
    output_sha256 = hashlib.sha256(args.output.read_bytes()).hexdigest()
    checksum_path = args.output.with_name(args.output.name + ".sha256")
    with checksum_path.open("x", encoding="utf-8") as checksum:
        checksum.write(f"{output_sha256}  {args.output.name}\n")
        checksum.flush()
        os.fsync(checksum.fileno())
    result["output_sha256"] = output_sha256
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
