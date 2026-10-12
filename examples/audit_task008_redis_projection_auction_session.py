"""Drive the shared Core auction session from a frozen Redis Q2 capture.

This offline audit uses an in-memory read-only client over an authenticated,
hash-pinned capture. It does not connect to Redis or convert the direct Core
input into Q2Frame. The older sealed-capture Q2Frame run is used only as a
comparison oracle for per-symbol anchor facts and session revision sets.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from engine_core import (  # noqa: E402
    AuctionAnchorSessionV1,
    FreshnessPolicy,
    RedisQ2ProjectionAdapter,
    build_auction_anchor_fact_v1,
)

CAPTURE_PATH = Path(
    "/home/exedev/validation/open-live-followup-20261009T094227+0800/"
    "probe_claude_live_capture.json"
)
LEGACY_RESULT_PATH = Path(
    "/home/exedev/validation/task008-late-start-anchor-observation-20261012T142736+0800/"
    "late_start_result.json"
)
EXPECTED_CAPTURE_SHA256 = (
    "7806d6d1dfd26daa4807ae0512708b21b37bf6c6d351104be364a70fd8d02003"
)
EXPECTED_LEGACY_RESULT_SHA256 = (
    "3e247a0a7915305339cf5dcaab0c92cdf878e7fba218e7ef6b15998177169aea"
)
ANCHOR_FIELDS = {"0920": "a20", "0924": "a24", "0925": "a25"}
SOURCE_LAYER = "redis_q2_sealed_hash_snapshot"
SOURCE_ID = "redis_q2_sealed_snapshot_20261009"


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


class SealedCaptureQ2Client:
    """Small read-only Q2 client implementing the Core Redis protocol."""

    def __init__(self, capture: Mapping[str, Any]) -> None:
        self.trade_date = str(capture["trade_date"])
        self.uncaptured_read_count = 0
        reads = capture.get("reads")
        if not isinstance(reads, list):
            raise ValueError("capture reads must be a list")
        self._sets: dict[str, tuple[Any, ...]] = {}
        self._hashes: dict[str, Mapping[Any, Any]] = {}
        for item in reads:
            if not isinstance(item, Mapping):
                raise ValueError("capture read item must be an object")
            operation = item.get("operation")
            key = item.get("key")
            if not isinstance(key, str):
                raise ValueError("capture read key must be a string")
            if operation == "smembers":
                value = item.get("value")
                if not isinstance(value, list):
                    raise ValueError("captured SMEMBERS result must be a list")
                if key in self._sets:
                    raise ValueError("duplicate captured SMEMBERS key")
                self._sets[key] = tuple(value)
            elif operation == "hgetall":
                value = item.get("value")
                if not isinstance(value, Mapping):
                    raise ValueError("captured HGETALL result must be an object")
                if key in self._hashes:
                    raise ValueError("duplicate captured HGETALL key")
                self._hashes[key] = dict(value)

    def smembers(self, key: str) -> Sequence[Any]:
        if key not in self._sets:
            self.uncaptured_read_count += 1
        return self._sets.get(key, ())

    def hgetall(self, key: str) -> Mapping[Any, Any]:
        if key not in self._hashes:
            self.uncaptured_read_count += 1
        return self._hashes.get(key, {})


def _parse_legacy_mapping(raw: Any) -> Mapping[str, Any]:
    if isinstance(raw, Mapping):
        return raw
    if not isinstance(raw, str):
        raise ValueError("legacy oracle facts must be a mapping")
    # Prior report serialization used repr() for MappingProxyType values.
    # Remove only that known wrapper before safe literal parsing.
    value = ast.literal_eval(raw.replace("mappingproxy(", "("))
    if not isinstance(value, Mapping):
        raise ValueError("legacy oracle facts did not decode to a mapping")
    return value


def run_audit(
    *, capture_path: Path, legacy_result_path: Path, out_dir: Path
) -> dict[str, Any]:
    capture_bytes = capture_path.read_bytes()
    capture_sha256 = _sha256(capture_bytes)
    if capture_sha256 != EXPECTED_CAPTURE_SHA256:
        raise ValueError(f"capture SHA-256 mismatch: {capture_sha256}")
    legacy_bytes = legacy_result_path.read_bytes()
    legacy_sha256 = _sha256(legacy_bytes)
    if legacy_sha256 != EXPECTED_LEGACY_RESULT_SHA256:
        raise ValueError(f"legacy oracle SHA-256 mismatch: {legacy_sha256}")
    capture = json.loads(capture_bytes)
    if capture.get("contract_version") != "RedisQ2ReadCaptureV1":
        raise ValueError("unsupported sealed Redis capture contract")
    trade_date = str(capture.get("trade_date", ""))
    observed_at_raw = capture.get("read_completed_at")
    if not isinstance(observed_at_raw, str):
        raise ValueError("capture read_completed_at is unavailable")
    observed_at = datetime.fromisoformat(observed_at_raw)
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("capture read_completed_at must be timezone-aware")

    client = SealedCaptureQ2Client(capture)
    snapshot = RedisQ2ProjectionAdapter(
        client,
        source_id=SOURCE_ID,
    ).read(
        trade_date,
        observed_at=observed_at,
        freshness_policy=FreshnessPolicy(),
    )
    if snapshot.envelope.effective_time_ms is None:
        raise ValueError("sealed Q2 projection has no effective source timestamp")
    evaluation_time_ms = snapshot.envelope.effective_time_ms
    evaluation_time_ms -= evaluation_time_ms % 1000
    observed_at_ms = snapshot.envelope.observed_time_ms
    rows = {
        symbol: {
            **quote.to_mapping(),
            "raw_fields": quote.raw_fields,
            "source_time_ms": quote.source_record_time_ms,
        }
        for symbol, quote in snapshot.quotes.items()
    }
    session = AuctionAnchorSessionV1(
        trade_date,
        snapshot.expected_symbols,
        signal_prefix="redis-q2-sealed-snapshot",
    )
    legacy = json.loads(legacy_bytes)
    legacy_anchors = legacy.get("ordered", {}).get("anchor_evidence", {})
    tag_results: dict[str, Any] = {}
    all_equal = True
    all_session_revision_sets_equal = True

    for tag, raw_field in ANCHOR_FIELDS.items():
        field_observed = any(
            raw_field in quote.raw_fields for quote in snapshot.quotes.values()
        )
        step = session.observe_late_start(
            tag,
            rows,
            logical_time_ms=evaluation_time_ms,
            anchor_field_observed=field_observed,
            observed_at_ms=observed_at_ms,
            source_layers=(SOURCE_LAYER,),
        )
        oracle_anchor = legacy_anchors.get(tag, {})
        oracle_facts = _parse_legacy_mapping(
            oracle_anchor.get("auction_anchor_facts_by_symbol", {})
        )
        direct_facts = {}
        for symbol in snapshot.expected_symbols:
            quote = snapshot.quotes.get(symbol)
            row = None
            if quote is not None:
                row = {
                    **quote.to_mapping(),
                    "raw_fields": quote.raw_fields,
                    "source_time_ms": quote.source_record_time_ms,
                }
            direct_facts[symbol] = build_auction_anchor_fact_v1(
                trade_date=trade_date,
                tag=tag,
                symbol=symbol,
                row=row,
                evaluation_time_ms=evaluation_time_ms,
                observed_at_ms=observed_at_ms,
                source_layer=SOURCE_ID,
                freeze_time_ms=evaluation_time_ms,
            ).as_trace()

        common = sorted(set(direct_facts) & set(oracle_facts))
        revision_available = set(
            step.revision.available_anchor_symbols if step.revision else ()
        )
        revision_missing = set(
            step.revision.missing_anchor_symbols if step.revision else ()
        )
        common_symbols = set(common)
        oracle_available = {
            symbol
            for symbol, fact in oracle_facts.items()
            if fact.get("status") == "AVAILABLE"
        }
        oracle_missing = {
            symbol
            for symbol, fact in oracle_facts.items()
            if fact.get("status") == "MISSING"
        }
        revision_available_common = revision_available & common_symbols
        oracle_available_common = oracle_available & common_symbols
        revision_missing_common = revision_missing & common_symbols
        oracle_missing_common = oracle_missing & common_symbols
        revision_available_equal = revision_available_common == oracle_available_common
        revision_missing_equal = revision_missing_common == oracle_missing_common
        all_session_revision_sets_equal = (
            all_session_revision_sets_equal
            and revision_available_equal
            and revision_missing_equal
        )
        mismatches = []
        same_count = 0
        for symbol in common:
            direct = direct_facts[symbol]
            previous = oracle_facts[symbol]
            pair = (direct.get("status"), direct.get("price_milli"))
            oracle_pair = (previous.get("status"), previous.get("price_milli"))
            if pair == oracle_pair:
                same_count += 1
            elif len(mismatches) < 20:
                mismatches.append({
                    "symbol": symbol,
                    "direct": {"status": pair[0], "price_milli": pair[1]},
                    "q2frame_oracle": {
                        "status": oracle_pair[0],
                        "price_milli": oracle_pair[1],
                    },
                })
        mismatch_count = len(common) - same_count
        all_equal = all_equal and mismatch_count == 0
        tag_results[tag] = {
            "late_start_status": step.status,
            "revision": step.revision.revision if step.revision else None,
            "revision_content_hash": step.revision.content_hash if step.revision else None,
            "timer_signal": (
                {
                    "signal_id": step.timer_signal.signal_id,
                    "logical_time_ms": step.timer_signal.logical_time_ms,
                    "signal_seq": step.timer_signal.signal_seq,
                    "signal_kind": step.timer_signal.signal_kind.value,
                    "payload": dict(step.timer_signal.payload),
                }
                if step.timer_signal
                else None
            ),
            "direct_expected_symbols": len(snapshot.expected_symbols),
            "direct_fact_count": len(direct_facts),
            "q2frame_oracle_fact_count": len(oracle_facts),
            "common_symbol_count": len(common),
            "revision_available_equal": revision_available_equal,
            "revision_only_count": len(
                revision_available_common - oracle_available_common
            ),
            "oracle_only_count": len(
                oracle_available_common - revision_available_common
            ),
            "revision_missing_equal": revision_missing_equal,
            "revision_missing_only_count": len(
                revision_missing_common - oracle_missing_common
            ),
            "oracle_missing_only_count": len(
                oracle_missing_common - revision_missing_common
            ),
            "same_status_and_price_count": same_count,
            "mismatch_count": mismatch_count,
            "mismatch_samples": mismatches,
            "direct_anchor_coverage": (
                step.revision.anchor_coverage if step.revision else None
            ),
            "direct_source_coverage": (
                step.revision.source_coverage if step.revision else None
            ),
        }

    manifest = {
        "contract": "Task008RedisProjectionAuctionSessionAuditV1",
        "classification": "ADAPTER_ROWS_TO_PER_SYMBOL_FACTS_AND_SESSION_REVISION_SETS",
        "trade_date": trade_date,
        "capture_path": str(capture_path),
        "capture_sha256": capture_sha256,
        "capture_observation_time_mode": capture.get("observation_time_mode"),
        "capture_read_completed_at": observed_at_raw,
        "legacy_q2frame_oracle_path": str(legacy_result_path),
        "legacy_q2frame_oracle_sha256": legacy_sha256,
        "projection_source_id": SOURCE_ID,
        "projection_status": snapshot.status.value,
        "projection_consistency_status": snapshot.consistency_status,
        "projection_expected_symbol_count": len(snapshot.expected_symbols),
        "projection_quote_count": len(snapshot.quotes),
        "projection_missing_symbol_count": len(snapshot.missing_symbols),
        "projection_uncaptured_read_count": client.uncaptured_read_count,
        "evaluation_time_ms": evaluation_time_ms,
        "observed_at_ms": observed_at_ms,
        "historical_available_at": "UNKNOWN_NOT_INFERRED",
        "redis_network_access": "NONE_SEALED_CAPTURE_ONLY",
        "td_rabbit_production_writes_effects": "NONE",
        "tag_results": tag_results,
        "all_common_symbol_status_price_equal": all_equal,
        "all_session_revision_sets_equal": all_session_revision_sets_equal,
        "code_sha256": {
            "src/engine_core/auction_session.py": _sha256_file(
                ROOT / "src/engine_core/auction_session.py"
            ),
            "examples/run_task008_q2frame_auction_engine_shadow.py": _sha256_file(
                ROOT / "examples/run_task008_q2frame_auction_engine_shadow.py"
            ),
            "examples/audit_task008_redis_projection_auction_session.py": _sha256_file(
                ROOT / "examples/audit_task008_redis_projection_auction_session.py"
            ),
        },
        "limits": [
            "sealed Redis capture is a real prior read cohort, not a live Redis connection",
            "Q2 per-symbol source timestamps are not anchor-field historical available_at",
            "the Q2Frame oracle was itself derived from this sealed capture as one late-start input group",
            "comparison covers adapter rows to per-symbol facts and session revision sets; not Engine strategy equivalence",
            "active Q2 membership is not independently proven to be full-market coverage",
        ],
    }
    out_dir.mkdir(parents=True, exist_ok=False)
    result_path = out_dir / "audit_result.json"
    result_path.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    report_lines = [
        "# TASK-008 Redis projection auction session audit",
        "",
        f"- Capture SHA-256: `{capture_sha256}`",
        f"- Q2Frame comparison oracle SHA-256: `{legacy_sha256}`",
        f"- Trade date / evaluation time: `{trade_date}` / `{evaluation_time_ms}`",
        f"- Projection expected members / quotes / missing hashes: `{len(snapshot.expected_symbols)}` / `{len(snapshot.quotes)}` / `{len(snapshot.missing_symbols)}`",
        f"- Shared session result: `{ 'PASS' if all_equal else 'MISMATCH' }`",
        f"- Session revision sets: `{ 'EQUAL' if all_session_revision_sets_equal else 'MISMATCH' }`",
        "- Comparison: adapter rows -> per-symbol fact builder, plus session revision sets (two comparisons).",
        "- Historical anchor-field `available_at`: `UNKNOWN_NOT_INFERRED`",
        "- Data access: local sealed Redis capture through `RedisQ2ProjectionAdapter`; no Redis network call.",
        "",
        "| Anchor | Status | Revision | Common symbols | Equal status+price | Revision sets equal | Mismatches |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for tag, item in tag_results.items():
        report_lines.append(
            f"| {tag} | {item['late_start_status']} | {item['revision']} | "
            f"{item['common_symbol_count']} | {item['same_status_and_price_count']} | "
            f"{item['revision_available_equal'] and item['revision_missing_equal']} | "
            f"{item['mismatch_count']} |"
        )
    report_lines.extend(["", "## Limits", "", *[f"- {item}" for item in manifest["limits"]], ""])
    report_path = out_dir / "audit_report.md"
    report_path.write_text("\n".join(report_lines), encoding="utf-8")
    sums_path = out_dir / "sha256sums.txt"
    files = sorted(path for path in out_dir.iterdir() if path.is_file())
    sums_path.write_text(
        "".join(f"{_sha256(path.read_bytes())}  {path.name}\n" for path in files),
        encoding="utf-8",
    )
    return {
        "out_dir": str(out_dir),
        "all_common_symbol_status_price_equal": all_equal,
        "all_session_revision_sets_equal": all_session_revision_sets_equal,
        "tag_results": {
            tag: {
                key: item[key]
                for key in (
                    "late_start_status",
                    "revision",
                    "common_symbol_count",
                    "revision_available_equal",
                    "revision_only_count",
                    "oracle_only_count",
                    "revision_missing_equal",
                    "revision_missing_only_count",
                    "oracle_missing_only_count",
                    "same_status_and_price_count",
                    "mismatch_count",
                )
            }
            for tag, item in tag_results.items()
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture", type=Path, default=CAPTURE_PATH)
    parser.add_argument("--legacy-oracle", type=Path, default=LEGACY_RESULT_PATH)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    outcome = run_audit(
        capture_path=args.capture,
        legacy_result_path=args.legacy_oracle,
        out_dir=args.out_dir,
    )
    print(json.dumps(outcome, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
