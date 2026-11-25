"""Analyze offline plate priority and cohesion artifacts without external data access."""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median
from typing import Any, Mapping

from engine_core.plate_rules import (
    collapse_runtime_primary_plate, is_generic_plate, normalize_plate_name, split_plate_tokens,
)


CATEGORIES = ("SAME", "REASON_OVER_STATIC", "GENERIC_REORDER", "ALIAS_COLLAPSE", "OTHER")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON input {path}: {exc}") from exc


def _artifact_dir(path: Path, filename: str, trade_date: str | None = None) -> Path:
    if (path / filename).is_file():
        return path
    if trade_date:
        candidate = path / trade_date
        if (candidate / filename).is_file():
            return candidate
    candidates = sorted(child for child in path.iterdir() if child.is_dir() and (child / filename).is_file()) if path.is_dir() else []
    if len(candidates) == 1:
        return candidates[0]
    raise ValueError(f"missing required input file: {filename} in {path}")


def load_inputs(
    memberships_dir: Path, cohesion_dir: Path, *, trade_date: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    member_dir = _artifact_dir(Path(memberships_dir), "memberships.json", trade_date)
    affinity_dir = _artifact_dir(Path(cohesion_dir), "affinity.json", trade_date)
    members = _read_json(member_dir / "memberships.json")
    affinity = _read_json(affinity_dir / "affinity.json")
    if not isinstance(members, Mapping) or not isinstance(members.get("by_symbol"), Mapping):
        raise ValueError("memberships.json must contain a by_symbol mapping")
    if not isinstance(affinity, list):
        raise ValueError("affinity.json must contain a list")
    return dict(members), affinity


def _snapshot_stock_plate(snapshot: Mapping[str, Any] | None) -> dict[str, str]:
    if snapshot is None:
        return {}
    raw = snapshot.get("stock_plate")
    if not isinstance(raw, Mapping):
        raise ValueError("snapshot must contain a stock_plate mapping")
    result = {}
    for symbol, value in raw.items():
        if isinstance(value, str) and value.strip():
            result[str(symbol)] = value.strip()
        elif isinstance(value, Mapping):
            plate = value.get("primary_plate", value.get("plate"))
            if isinstance(plate, str) and plate.strip():
                result[str(symbol)] = plate.strip()
    return result


def _legacy_primary_by_symbol(memberships: Mapping[str, Any]) -> dict[str, str]:
    raw = memberships.get("legacy_weights", {})
    direct = memberships.get("legacy_v1_primary_by_symbol")
    if isinstance(direct, Mapping) and direct:  # written by run_offline_plate_pipeline (C078)
        return {str(k): split_plate_tokens(v)[0] for k, v in direct.items() if split_plate_tokens(v)}
    if not isinstance(raw, Mapping):
        return {}
    result = {}
    for symbol, weights in raw.items():
        if isinstance(weights, list) and weights:
            row = weights[0]
            if isinstance(row, (list, tuple)) and row and row[0]:
                plate = split_plate_tokens(row[0])
                if plate:
                    result[str(symbol)] = plate[0]
    return result


def _stored_primary_by_symbol(snapshot: Mapping[str, Any] | None) -> tuple[dict[str, str], dict[str, str]]:
    raw_values = _snapshot_stock_plate(snapshot)
    raw_primary = {}
    normalized = {}
    for symbol, raw in raw_values.items():
        tokens = split_plate_tokens(raw)
        if tokens:
            raw_primary[symbol] = re.split(r"[+,，、/|；;]+", raw, maxsplit=1)[0].strip()
            normalized[symbol] = tokens[0]
    return raw_primary, normalized


def _rank1_by_symbol(memberships: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    result = {}
    for symbol, rows in memberships.get("by_symbol", {}).items():
        if isinstance(rows, list):
            row = next((row for row in rows if isinstance(row, Mapping) and row.get("rank_in_stock") == 1), None)
            if row is not None:
                plate = split_plate_tokens(row.get("plate"))
                if plate:
                    result[str(symbol)] = {**row, "plate": plate[0]}
    return result


def _reconciliation_reason(
    raw_stored: str, stored: str, legacy: str, rank1: Mapping[str, Any],
) -> str:
    if normalize_plate_name(raw_stored) != raw_stored and stored != legacy:
        return "BRACKET_CLEAN"
    if collapse_runtime_primary_plate(stored) == collapse_runtime_primary_plate(legacy):
        return "ALIAS"
    if is_generic_plate(stored) != is_generic_plate(legacy):
        return "GENERIC"
    if rank1.get("tier") in (1, 2):
        return "POOL_OR_REASON"
    return "OTHER"


def _reconciliation(memberships: Mapping[str, Any], snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
    raw_stored, stored = _stored_primary_by_symbol(snapshot)
    legacy = _legacy_primary_by_symbol(memberships)
    rank1 = _rank1_by_symbol(memberships)
    symbols = sorted(set(raw_stored) | set(stored) | set(legacy) | set(rank1))
    comparable = sorted(set(stored) & set(legacy) & set(rank1))
    counts = {
        "stored_legacy_primary_missing": len(set(symbols) - set(stored)),
        "legacy_v1_primary_missing": len(set(symbols) - set(legacy)),
        "user_priority_rank1_missing": len(set(symbols) - set(rank1)),
    }
    mismatches: dict[str, list[dict[str, str]]] = {
        key: [] for key in ("BRACKET_CLEAN", "ALIAS", "GENERIC", "POOL_OR_REASON", "OTHER")
    }
    equal_counts = {"stored_eq_legacy_v1": 0, "stored_eq_user": 0, "legacy_v1_eq_user": 0}
    for symbol in comparable:
        s, l, u = stored[symbol], legacy[symbol], rank1[symbol]["plate"]
        equal_counts["stored_eq_legacy_v1"] += s == l
        equal_counts["stored_eq_user"] += s == u
        equal_counts["legacy_v1_eq_user"] += l == u
        if s != l:
            reason = _reconciliation_reason(raw_stored[symbol], s, l, rank1[symbol])
            mismatches[reason].append({"symbol": symbol, "stored": s, "legacy_v1": l,
                                       "user_priority_rank1": u})
    n = len(comparable)
    shares = {key + "_share": (count / n if n else None) for key, count in equal_counts.items()}
    return {
        "n_comparable": n,
        **equal_counts,
        **shares,
        "excluded_missing_counts": counts,
        "stored_vs_legacy_v1_reason_counts": {key: len(rows) for key, rows in mismatches.items()},
        "stored_vs_legacy_v1_reason_examples": {key: rows[:15] for key, rows in mismatches.items()},
    }


def _category(new: str, old: str, rank1: Mapping[str, Any]) -> str:
    if new == old:
        return "SAME"
    if collapse_runtime_primary_plate(new) == collapse_runtime_primary_plate(old):
        return "ALIAS_COLLAPSE"
    if rank1.get("tier") == 2:
        return "REASON_OVER_STATIC"
    new_generic = bool(rank1.get("generic", is_generic_plate(new)))
    old_generic = is_generic_plate(old)
    if new_generic != old_generic or new_generic:
        return "GENERIC_REORDER"
    return "OTHER"


def _priority_analysis(memberships: Mapping[str, Any], stock_plate: Mapping[str, str]) -> dict[str, Any]:
    by_symbol = memberships["by_symbol"]
    raw_legacy = memberships.get("legacy_weights", {})
    categories: dict[str, list[dict[str, str]]] = {category: [] for category in CATEGORIES}
    missing = {"rank1_missing": 0, "legacy_primary_missing": 0}
    if not stock_plate and isinstance(raw_legacy, Mapping):
        for symbol, weights in raw_legacy.items():
            if isinstance(weights, list) and weights and isinstance(weights[0], list) and weights[0]:
                stock_plate[str(symbol)] = str(weights[0][0])
    for symbol, rows in sorted(by_symbol.items(), key=lambda item: str(item[0])):
        rank1 = next((row for row in rows if isinstance(row, Mapping) and row.get("rank_in_stock") == 1), None) if isinstance(rows, list) else None
        if rank1 is None:
            missing["rank1_missing"] += 1
            continue
        old = stock_plate.get(str(symbol))
        if not old:
            missing["legacy_primary_missing"] += 1
            continue
        new = str(rank1.get("plate") or "")
        category = _category(new, old, rank1)
        categories[category].append({"symbol": str(symbol), "user_priority": new, "legacy": old})
    comparable = sum(map(len, categories.values()))
    return {
        "comparable_count": comparable,
        "missing_counts": missing,
        "categories": {
            name: {"count": len(rows), "share": len(rows) / comparable if comparable else None,
                  "examples": rows[:20]}
            for name, rows in categories.items()
        },
    }


def _reason_cohesion(memberships: Mapping[str, Any], affinity: list[dict[str, Any]]) -> dict[str, Any]:
    affinity_by_pair = {}
    for row in affinity:
        if isinstance(row, Mapping) and row.get("symbol") is not None and row.get("plate") is not None:
            affinity_by_pair[(str(row["symbol"]), str(row["plate"]))] = row
    tier_rows: dict[int, list[tuple[str, str, Mapping[str, Any] | None]]] = {2: [], 3: []}
    no_evidence: dict[str, set[str]] = defaultdict(set)
    reason_symbols = set()
    missing_by_tier = Counter()
    for symbol, rows in memberships["by_symbol"].items():
        if not isinstance(rows, list):
            continue
        for member in rows:
            if not isinstance(member, Mapping) or member.get("tier") not in (2, 3):
                continue
            tier, plate = int(member["tier"]), str(member.get("plate") or "")
            evidence = affinity_by_pair.get((str(symbol), plate))
            tier_rows[tier].append((str(symbol), plate, evidence))
            if tier == 2:
                reason_symbols.add(str(symbol))
                corr = evidence.get("best_corr") if evidence else None
                role = evidence.get("role") if evidence else None
                valid_corr = isinstance(corr, (int, float)) and not isinstance(corr, bool) and math.isfinite(float(corr))
                if evidence is None or role == "UNKNOWN" or not valid_corr or abs(float(corr)) < 0.3:
                    no_evidence[str(symbol)].add(plate)
            if evidence is None:
                missing_by_tier[tier] += 1
    stats = {}
    for tier, entries in tier_rows.items():
        observed = [row for _, _, row in entries if row is not None]
        role_counts = Counter(str(row.get("role")) for row in observed if row.get("role") is not None)
        unknown_count = sum(row.get("role") == "UNKNOWN" for row in observed)
        correlations = [abs(float(row["best_corr"])) for row in observed
                        if isinstance(row.get("best_corr"), (int, float)) and not isinstance(row.get("best_corr"), bool)
                        and math.isfinite(float(row["best_corr"]))]
        stats[f"tier_{tier}"] = {
            "membership_count": len(entries), "affinity_count": len(observed),
            "missing_affinity_count": missing_by_tier[tier],
            "role_counts": dict(sorted(role_counts.items())),
            "role_missing_count": sum(row.get("role") is None for row in observed),
            "unknown_count": unknown_count,
            "unknown_share": unknown_count / len(observed) if observed else None,
            "best_corr_count": len(correlations),
            "abs_best_corr_median": median(correlations) if correlations else None,
        }
    return {
        **stats,
        "no_cluster_evidence_symbol_count": len(no_evidence),
        "no_cluster_evidence_symbols": [
            {"symbol": symbol, "reason_plates": sorted(no_evidence[symbol])}
            for symbol in sorted(no_evidence)[:30]
        ],
        "reason_symbol_count": len(reason_symbols),
    }


def analyze(
    memberships: Mapping[str, Any], affinity: list[dict[str, Any]], *,
    snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    stock_plate = _snapshot_stock_plate(snapshot)
    priority = _priority_analysis(memberships, stock_plate)
    return {
        "interpretation": "离线事实, 非策略结论",
        "priority_difference": priority,
        "reconciliation": _reconciliation(memberships, snapshot),
        "reason_cohesion": _reason_cohesion(memberships, affinity),
    }


def ensure_empty_output_dir(path: Path) -> None:
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise ValueError(f"output directory must be empty: {path}")
    path.mkdir(parents=True, exist_ok=True)


def write_outputs(output_dir: Path, result: Mapping[str, Any]) -> None:
    ensure_empty_output_dir(output_dir)
    (output_dir / "analysis.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    (output_dir / "README").write_text(
        "# Plate priority and cohesion analysis\n\n"
        "口径：`priority_difference` 的 SAME 等分类比较 USER_PRIORITY_V1 rank1 与快照 stock_plate 原始主板块，反映旧存储值口径；流水线 `legacy_v1_primary_match_rate` 比较 USER_PRIORITY_V1 rank1 与同一份输入重算的 LEGACY_V1 主板块。两者参照对象不同，不能直接视作同一一致率。新增 `reconciliation` 在 snapshot、LEGACY_V1 legacy_weights 和 USER_PRIORITY_V1 rank1 均有主板块的交集上，对三者逐项对账；快照主板块先拆词、取首项并规范化。缺失任一方的个股计入 excluded_missing_counts，不进入比例。\n\n"
        "原因板块聚集性按 tier 2 与 tier 3 的 affinity role、|best_corr| 和 UNKNOWN 占比统计。缺失 affinity、缺失相关系数及不可比较的主板块单独计数，不补零。\n\n"
        "本产物是离线事实非策略结论。\n", encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memberships-dir", type=Path, required=True, help="pipeline output directory or a date directory")
    parser.add_argument("--cohesion-dir", type=Path, required=True, help="cohesion output directory")
    parser.add_argument("--trade-date", help="select one date directory when pipeline output contains multiple dates")
    parser.add_argument("--snapshot", type=Path, help="legacy snapshot JSON or directory containing stock_plate data")
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        memberships, affinity = load_inputs(args.memberships_dir, args.cohesion_dir, trade_date=args.trade_date)
        snapshot = None
        if args.snapshot:
            snapshot_path = args.snapshot
            if snapshot_path.is_dir():
                candidates = (snapshot_path / "legacy_snapshot.json", snapshot_path / "snapshot.json")
                source = next((item for item in candidates if item.is_file()), None)
                if source:
                    snapshot = _read_json(source)
                else:
                    stock = snapshot_path / "stock_plate.json"
                    if not stock.is_file():
                        raise ValueError("snapshot directory must contain stock_plate.json or legacy_snapshot.json")
                    snapshot = {"stock_plate": _read_json(stock)}
            else:
                snapshot = _read_json(snapshot_path)
        write_outputs(args.output_dir, analyze(memberships, affinity, snapshot=snapshot))
    except ValueError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
