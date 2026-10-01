"""Compare the legacy and Core opening plate-amount facts on pinned inputs.

This audit reads the real historical ``auction_snapshot_v2`` projection with
TD SELECT only, a date-frozen stock-to-plate mapping, an exact-release t1-v2
Q2Frame, and the matching Core opening report. It writes evidence only below
the requested validation directory. It never writes Redis/TD, consumes
RabbitMQ, restarts services, or emits effects.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from engine_core import (  # noqa: E402
    build_opening_plate_amount_summary,
    canonical_json,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
TD_FIELDS = (
    "ts",
    "px_milli",
    "chg_bp",
    "match_amt_yuan",
    "rest_bid_amt_yuan",
    "rest_ask_amt_yuan",
    "limit_state",
    "symbol",
    "trade_date",
    "auction_tag",
)
DEFAULT_ENGINE_NEXT_RELEASE = Path(
    "/home/exedev/services/engine-next/releases/20260903_e272842"
)
DEFAULT_MAPPING_SNAPSHOT = Path(
    "/home/exedev/services/engine-next/shared/runtime_state/2026-09-29/stock_plate_snapshot.json"
)
DEFAULT_Q2FRAME = Path(
    "/home/exedev/validation/task008-opening-q2frame-20260929-20260930T004859+0800/deployed_release_q2frame_to_0932.jsonl"
)
DEFAULT_CORE_REPORT = Path(
    "/home/exedev/validation/task008-opening-q2frame-20260929-20260930T004859+0800/core_auction_opening_shadow.json"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, default=str)
        + "\n",
        encoding="utf-8",
    )


def _safe_trade_date(value: str) -> str:
    parsed = datetime.strptime(value, "%Y-%m-%d")
    if parsed.strftime("%Y-%m-%d") != value:
        raise ValueError("trade-date must be YYYY-MM-DD")
    return value


def _td_connection_settings(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Require an explicitly configured TD endpoint and account.

    The audit is SELECT-only, but silently falling back to a local superuser
    account can connect to the wrong TD instance or use unnecessary privilege.
    Never include supplied credential values in validation errors.
    """

    source = os.environ if environ is None else environ
    required = ("TDENGINE_HOST", "TDENGINE_PORT", "TDENGINE_USER", "TDENGINE_PASSWORD")
    missing = [name for name in required if not source.get(name)]
    if missing:
        raise ValueError(
            "explicit TD connection settings required: " + ", ".join(missing)
        )
    try:
        port = int(source["TDENGINE_PORT"])
    except (TypeError, ValueError) as exc:
        raise ValueError("TDENGINE_PORT must be an integer from 1 to 65535") from exc
    if not 1 <= port <= 65535:
        raise ValueError("TDENGINE_PORT must be an integer from 1 to 65535")
    host = source["TDENGINE_HOST"].strip()
    user = source["TDENGINE_USER"].strip()
    password = source["TDENGINE_PASSWORD"]
    if not host or not user or not password:
        raise ValueError("TD connection host, user, and password must be non-empty")
    return {"host": host, "port": port, "user": user, "password": password}


def _connect_and_read_td_rows(*, trade_date: str, database: str) -> list[dict[str, Any]]:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", database):
        raise ValueError("database must be a simple SQL identifier")
    import taos  # type: ignore[import-not-found]

    connection = taos.connect(**_td_connection_settings(), database=database)
    compact_date = trade_date.replace("-", "")
    sql = (
        "SELECT "
        + ", ".join(TD_FIELDS)
        + f" FROM {database}.auction_snapshot_v2"
        + f' WHERE trade_date="{compact_date}"'
        + ' AND auction_tag IN ("0920","0924","0925")'
        + " ORDER BY ts, symbol, auction_tag"
    )
    try:
        cursor = connection.cursor()
        cursor.execute(sql)
        raw_rows = cursor.fetchall()
    finally:
        connection.close()

    rows: list[dict[str, Any]] = []
    for raw in raw_rows:
        if len(raw) != len(TD_FIELDS):
            raise ValueError("TD auction row has an unexpected column count")
        row = dict(zip(TD_FIELDS, raw))
        row["ts"] = row["ts"].isoformat() if isinstance(row.get("ts"), datetime) else row.get("ts")
        row["auction_tag"] = str(row.get("auction_tag") or "").strip()
        row["symbol"] = str(row.get("symbol") or "").strip()
        rows.append(row)
    return rows


def _frame_rows(path: Path):
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            if not line.strip():
                continue
            frame = json.loads(line)
            if not isinstance(frame, Mapping):
                raise ValueError(f"Q2Frame row {line_no} is not an object")
            yield frame


def _compare_plates(
    legacy_rows: list[Mapping[str, Any]], core_rows: list[Mapping[str, Any]]
) -> dict[str, Any]:
    def index_unique(rows: list[Mapping[str, Any]], label: str) -> dict[str, Mapping[str, Any]]:
        result: dict[str, Mapping[str, Any]] = {}
        for row in rows:
            plate = str(row.get("plate") or "").strip()
            if not plate:
                raise ValueError(f"{label} plate identifiers must be non-empty")
            if plate in result:
                raise ValueError(f"{label} plate identifiers must be unique")
            result[plate] = row
        return result

    legacy_by_plate = index_unique(legacy_rows, "legacy")
    core_by_plate = index_unique(core_rows, "Core")
    fields = (
        "open_valid_count",
        "common_symbol_count",
        "comparison_valid_count",
        "open_window_amount_yuan",
        "open_window_amount_status",
        "open_amount_present_count",
        "open_amount_total_count",
        "comparison_amount_status",
        "comparison_amount_present_count",
        "comparison_amount_total_count",
        "open_top1_amount_ratio",
        "open_top3_amount_ratio",
        "auction_top1_amount_ratio",
        "top1_amount_ratio_delta",
        "concentration_state",
        "open_symbols",
    )
    mismatches: list[dict[str, Any]] = []
    for plate in sorted(set(legacy_by_plate) | set(core_by_plate)):
        if plate not in legacy_by_plate or plate not in core_by_plate:
            mismatches.append(
                {
                    "plate": plate,
                    "field": "plate_membership",
                    "legacy_present": plate in legacy_by_plate,
                    "core_present": plate in core_by_plate,
                }
            )
            continue
        for field in fields:
            if legacy_by_plate[plate].get(field) != core_by_plate[plate].get(field):
                mismatches.append(
                    {
                        "plate": plate,
                        "field": field,
                        "legacy": legacy_by_plate[plate].get(field),
                        "core": core_by_plate[plate].get(field),
                    }
                )
    return {
        "compared_plate_count": len(set(legacy_by_plate) & set(core_by_plate)),
        "compared_fields_per_plate": list(fields),
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
        "classification": "STRICT_VALUE_MATCH" if not mismatches else "VALUE_MISMATCH",
    }


def run_audit(
    *,
    trade_date: str,
    output_dir: Path,
    engine_next_release: Path,
    mapping_snapshot_path: Path,
    q2frame_path: Path,
    core_report_path: Path,
    database: str,
) -> dict[str, Any]:
    trade_date = _safe_trade_date(trade_date)
    if output_dir.exists() and any(output_dir.iterdir()):
        raise FileExistsError("output directory must be new or empty")
    output_dir.mkdir(parents=True, exist_ok=True)

    sys.path.insert(0, str(engine_next_release))
    from engine_next.runtime.auction_email_report import build_auction_fact_view
    from engine_next.runtime.auction_shadow import build_plate_shadow_from_snapshot_rows
    from engine_next.runtime.open_confirmation import (
        _auction_symbols_by_plate,
        _mapped_symbols_by_plate,
        build_observation_from_inputs,
    )
    from engine_next.runtime.production_fact_assembly import (
        load_mapping_snapshot,
        normalize_td_auction_row,
    )

    mapping_root = mapping_snapshot_path.parent.parent
    frozen_mapping = load_mapping_snapshot(
        directory=mapping_root,
        trade_date=trade_date,
        minimum_record_count=0,
    )
    if frozen_mapping is None:
        raise ValueError("the exact-date runtime stock-to-plate snapshot is missing")
    mapping = dict(frozen_mapping["mapping"])
    rows = _connect_and_read_td_rows(trade_date=trade_date, database=database)
    row_counts = Counter(row["auction_tag"] for row in rows)
    normalized = [
        normalize_td_auction_row(row, tag=row["auction_tag"])
        for row in rows
    ]
    mapping_origin = {
        "canonical": "market:stock_plate",
        "status": "runtime_owned_snapshot",
        "trade_date": trade_date,
        "effective_time": frozen_mapping.get("effective_time"),
        "sha256": frozen_mapping.get("sha256"),
    }
    shadow = build_plate_shadow_from_snapshot_rows(
        normalized,
        trade_date=trade_date,
        stock_plate=mapping,
        data_origin="replay_fixture_only",
        mapping_origin=mapping_origin,
        historical_valid=False,
    )

    core_report = json.loads(core_report_path.read_text(encoding="utf-8"))
    q2frame_sha256 = _sha256(q2frame_path)
    if core_report.get("trade_date") != trade_date:
        raise ValueError("Core report trade_date does not match TD snapshot")
    if core_report.get("q2frame", {}).get("sha256") != q2frame_sha256:
        raise ValueError("Core report does not identify the supplied Q2Frame")
    opening = core_report["ordered"]["opening_evidence"]["OPENING_0932"]
    facts_by_symbol = opening["facts_by_symbol"]
    evaluation_ms = int(opening["evaluation_time_ms"])
    cutoff = datetime.fromtimestamp(evaluation_ms / 1000, tz=timezone.utc).astimezone(SHANGHAI)

    auction_evidence = {
        "trade_date": trade_date,
        "data_origin": "replay_fixture_only",
        "component_statuses": {
            "market_overview": "unavailable",
            "plate_facts": "partial",
        },
    }
    auction_view = build_auction_fact_view(
        plate_shadow=shadow,
        auction_evidence=auction_evidence,
    )
    legacy = build_observation_from_inputs(
        auction_evidence=auction_evidence,
        plate_shadow=shadow,
        open_q2_rows=_frame_rows(q2frame_path),
        observation_cutoff=cutoff,
        data_origin="replay_fixture_only",
        open_q2_format="q2frame",
        open_q2_meta={
            "universe_authority_status": "partial",
            "source": "exact-release t1-v2 historical Q2Frame",
        },
    )

    selected_plates = [str(row.get("plate") or "") for row in legacy.get("plates", [])]
    auction_ratios = {
        str(row.get("plate") or ""): row.get("top1_amount_ratio")
        for row in auction_view.get("plate_rows", [])
        if row.get("plate")
    }
    core_summary = build_opening_plate_amount_summary(
        facts_by_symbol,
        mapped_symbols_by_plate=_mapped_symbols_by_plate(shadow),
        auction_symbols_by_plate=_auction_symbols_by_plate(shadow),
        auction_top1_amount_ratio_by_plate=auction_ratios,
        selected_plates=selected_plates,
    )
    comparison = _compare_plates(legacy.get("plates", []), core_summary["plates"])

    td_input_path = output_dir / "td_auction_snapshot_rows.jsonl"
    with td_input_path.open("x", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(canonical_json(row) + "\n")
    mapping_copy_path = output_dir / "stock_plate_snapshot.json"
    mapping_copy_path.write_text(
        mapping_snapshot_path.read_text(encoding="utf-8"), encoding="utf-8", newline="\n"
    )
    _write_json(output_dir / "legacy_open_confirmation.json", legacy)
    _write_json(output_dir / "core_opening_plate_amount_summary.json", core_summary)

    anchor_stats: dict[str, Any] = {}
    for tag in ("0920", "0924", "0925"):
        tag_rows = [row for row in rows if row["auction_tag"] == tag]
        symbols = {row["symbol"] for row in tag_rows if row["symbol"]}
        prices_present = sum(row.get("px_milli") is not None for row in tag_rows)
        anchor_stats[tag] = {
            "rows": len(tag_rows),
            "unique_symbols": len(symbols),
            "duplicate_symbol_rows": len(tag_rows) - len(symbols),
            "price_present": prices_present,
            "mapping_overlap": len(symbols & set(mapping)),
        }

    comparison["status"] = (
        "PASS_WITH_LIMITS" if comparison["classification"] == "STRICT_VALUE_MATCH" else "VALUE_MISMATCH"
    )
    comparison["trade_date"] = trade_date
    comparison["observation_cutoff"] = cutoff.isoformat()
    comparison["data_origin"] = "historical event-time replay with real TD auction rows and frozen mapping"
    comparison["scope_limits"] = [
        "Q2Frame is a t1-v2 event-time replay, not the original Rabbit arrival/processing sequence.",
        "The frozen mapping and auction projection are real same-date inputs; this does not prove full-market authority.",
        "M3-1 NORMAL and TD write health are outside this fact-slice audit.",
    ]
    comparison["inputs"] = {
        "td_source": "market_data1.auction_snapshot_v2",
        "td_query_kind": "SELECT_ONLY",
        "td_row_count": len(rows),
        "td_rows_by_tag": dict(sorted(row_counts.items())),
        "td_rows_sha256": _sha256(td_input_path),
        "mapping_snapshot_path": str(mapping_snapshot_path),
        "mapping_snapshot_sha256": _sha256(mapping_snapshot_path),
        "mapping_data_sha256": frozen_mapping.get("sha256"),
        "mapping_effective_time": frozen_mapping.get("effective_time"),
        "mapping_record_count": frozen_mapping.get("record_count"),
        "q2frame_path": str(q2frame_path),
        "q2frame_sha256": q2frame_sha256,
        "core_report_path": str(core_report_path),
        "core_report_sha256": _sha256(core_report_path),
        "engine_next_release": str(engine_next_release),
        "engine_next_open_confirmation_sha256": _sha256(engine_next_release / "engine_next/runtime/open_confirmation.py"),
        "engine_next_auction_shadow_sha256": _sha256(engine_next_release / "engine_next/runtime/auction_shadow.py"),
        "anchor_inventory": anchor_stats,
    }
    comparison["side_effects"] = {
        "td": "SELECT_ONLY",
        "redis_writes": 0,
        "rabbit_consume_or_ack": False,
        "service_changes": False,
        "effects": False,
        "files_written": "validation output directory only",
    }
    _write_json(output_dir / "opening_plate_amount_parity.json", comparison)

    artifact_hashes = {
        path.name: _sha256(path)
        for path in sorted(output_dir.iterdir())
        if path.is_file() and path.name != "sha256sums.txt"
    }
    (output_dir / "sha256sums.txt").write_text(
        "".join(f"{digest}  {name}\n" for name, digest in artifact_hashes.items()),
        encoding="utf-8",
    )
    return comparison


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", default="2026-09-29")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--engine-next-release", type=Path, default=DEFAULT_ENGINE_NEXT_RELEASE)
    parser.add_argument("--mapping-snapshot", type=Path, default=DEFAULT_MAPPING_SNAPSHOT)
    parser.add_argument("--q2frame", type=Path, default=DEFAULT_Q2FRAME)
    parser.add_argument("--core-report", type=Path, default=DEFAULT_CORE_REPORT)
    parser.add_argument("--td-database", default=os.environ.get("TDENGINE_DATABASE", "market_data1"))
    args = parser.parse_args()
    result = run_audit(
        trade_date=args.trade_date,
        output_dir=args.output_dir,
        engine_next_release=args.engine_next_release,
        mapping_snapshot_path=args.mapping_snapshot,
        q2frame_path=args.q2frame,
        core_report_path=args.core_report,
        database=args.td_database,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
