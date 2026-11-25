"""Point-in-time yesterday-streak feedback and promotion matrices."""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

from .limit_price import is_limit_down, is_limit_up, limit_prices, touched_limit


from .live_session import OPEN_CHECKPOINTS

CHECKPOINTS = tuple(label for label, _ in OPEN_CHECKPOINTS)
STATUSES = ("PROMOTED", "HIT_LIMIT_NOT_SEALED", "BROKEN", "FELL_BACK", "LIMIT_DOWN", "UNKNOWN")


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
        return parsed if parsed == parsed and abs(parsed) != float("inf") else None
    except (TypeError, ValueError, OverflowError):
        return None


def _symbol(row: Mapping[str, Any]) -> str:
    digits = "".join(ch for ch in str(row.get("symbol", row.get("code", ""))) if ch.isdigit())
    return digits[-6:].zfill(6) if digits else ""


def _sealed(snapshot: Mapping[str, Any], symbol: str) -> bool:
    # Producer limit state cannot substitute for missing or contradictory price data.
    price, pre_close = _number(snapshot.get("price")), _number(snapshot.get("pre_close"))
    rest_bid = _number(snapshot.get("rest_bid", snapshot.get("rest_bid_yuan")))
    return (price is not None and pre_close is not None and rest_bid is not None and rest_bid > 0
            and is_limit_up(price, pre_close, symbol,
                            is_st=bool(snapshot.get("is_st")),
                            unlimited=bool(snapshot.get("unlimited_price_band"))))


def _state(symbol: str, snapshot: Mapping[str, Any], prior_sealed: bool) -> str:
    price = _number(snapshot.get("price"))
    pre_close = _number(snapshot.get("pre_close"))
    rest_bid = _number(snapshot.get("rest_bid", snapshot.get("rest_bid_yuan")))
    rest_ask = _number(snapshot.get("rest_ask", snapshot.get("rest_ask_yuan")))
    high = _number(snapshot.get("high"))
    limit_state = snapshot.get("limit_state", snapshot.get("ls"))
    try:
        limit_state = int(limit_state) if limit_state is not None else None
    except (TypeError, ValueError, OverflowError):
        limit_state = None

    # Mutually exclusive priority: down-limit event, observed prior seal then loss,
    # current sealed promotion, touched without a seal, known no-touch fallback.
    if limit_state == -1 or (price is not None and pre_close is not None and rest_ask is not None
                             and rest_ask > 0 and is_limit_down(price, pre_close, symbol)):
        return "LIMIT_DOWN"
    if (prior_sealed and price is not None and pre_close is not None and rest_bid is not None
            and not _sealed(snapshot, symbol)):
        return "BROKEN"
    if _sealed(snapshot, symbol):
        return "PROMOTED"
    if price is None or pre_close is None or limit_prices(pre_close, symbol) is None:
        return "UNKNOWN"
    if high is not None and touched_limit(high, pre_close, symbol):
        return "HIT_LIMIT_NOT_SEALED" if rest_bid is not None else "UNKNOWN"
    if high is None or rest_bid is None:
        return "UNKNOWN"
    return "FELL_BACK"


def _stats(rows: Sequence[Mapping[str, Any]], *, with_return: bool = False) -> dict[str, Any]:
    counts = {status.lower(): sum(row.get("status") == status for row in rows) for status in STATUSES}
    total = len(rows)
    evaluable = total - counts["unknown"]
    counts.update({"total": total, "evaluable": evaluable,
                   "promotion_rate": counts["promoted"] / total if total else None})
    if with_return:
        returns = sorted(value for row in rows if (value := _number(row.get("return_pct"))) is not None)
        n = len(returns)
        counts.update({"return_count": n,
                       "mean_return_pct": sum(returns) / n if n else None,
                       "median_return_pct": ((returns[(n - 1) // 2] + returns[n // 2]) / 2) if n else None,
                       "red_share": sum(value > 0 for value in returns) / n if n else None})
    return counts


def build_streak_feedback(*, trade_date: str, used_date: str, version: str | None,
                          pool: Sequence[Mapping[str, Any]],
                          snapshots: Mapping[str, Mapping[str, Mapping[str, Any]]],
                          stage: str,
                          checkpoint: str | None = None,
                          theme_assignments: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Build per-checkpoint D09 rows and D10 board-height/theme matrices.

    A checkpoint only receives an observed status when its own quote has the
    needed fields. Auction status remains provisional. The visible pool is the
    denominator; UNKNOWN remains an explicit matrix column.
    """
    normalized_pool: dict[str, dict[str, Any]] = {}
    for raw in pool:
        symbol = _symbol(raw)
        if not symbol:
            continue
        try:
            level = int(raw.get("lb_days", raw.get("streak", raw.get("prior_streak"))))
        except (TypeError, ValueError, OverflowError):
            continue
        if level > 0:
            normalized_pool[symbol] = {**dict(raw), "symbol": symbol, "lb_days": level}
    assignments = theme_assignments or {}
    checkpoints: dict[str, Any] = {}
    visible_checkpoints = CHECKPOINTS
    if stage == "open" and checkpoint is not None:
        if checkpoint not in CHECKPOINTS:
            raise ValueError(f"unknown feedback checkpoint: {checkpoint}")
        visible_checkpoints = CHECKPOINTS[:CHECKPOINTS.index(checkpoint) + 1]
    for checkpoint_label in visible_checkpoints:
        source = snapshots.get(checkpoint_label, {})
        rows: list[dict[str, Any]] = []
        for symbol, pool_row in sorted(normalized_pool.items()):
            quote = source.get(symbol)
            quote = quote if isinstance(quote, Mapping) else {}
            index = CHECKPOINTS.index(checkpoint_label)
            prior_sealed = any(
                isinstance(snapshots.get(previous_checkpoint, {}).get(symbol), Mapping)
                and _sealed(snapshots[previous_checkpoint][symbol], symbol)
                for previous_checkpoint in CHECKPOINTS[:index]
            ) or bool(quote.get("prior_seal_observed"))
            assignment = assignments.get(symbol, {})
            primary = assignment.get("primary_theme") if isinstance(assignment, Mapping) else None
            state = _state(symbol, quote, prior_sealed)
            if not quote:
                reason = "该截点无行情"
            elif _number(quote.get("price")) is None or _number(quote.get("pre_close")) is None:
                reason = "价格或昨收缺失"
            elif state == "UNKNOWN":
                reason = "缺少封单或截至截点最高价路径，不能区分触板与回落"
            else:
                reason = quote.get("reason")
            rows.append({
                "symbol": symbol, "name": pool_row.get("name"), "lb_days": pool_row["lb_days"],
                "status": state,
                "price": _number(quote.get("price")), "return_pct": _number(quote.get("return_pct", quote.get("pct_change"))),
                "rest_bid": _number(quote.get("rest_bid", quote.get("rest_bid_yuan"))),
                "rest_ask": _number(quote.get("rest_ask", quote.get("rest_ask_yuan"))),
                "amount": _number(quote.get("amount", quote.get("amt_yuan", quote.get("match_amount_yuan")))),
                "primary_theme_id": primary.get("id") if isinstance(primary, Mapping) else None,
                "primary_theme": primary.get("name") if isinstance(primary, Mapping) else None,
                "reason": reason,
            })
        by_level: dict[str, list[dict[str, Any]]] = defaultdict(list)
        by_theme: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            by_level[f"{row['lb_days']}板"].append(row)
            if row.get("primary_theme_id"):
                by_theme[str(row["primary_theme_id"])].append(row)
        level_stats = {key: _stats(group) for key, group in sorted(by_level.items(), key=lambda item: int(item[0][:-1]))}
        theme_stats: dict[str, Any] = {}
        for key, group in sorted(by_theme.items()):
            primary = next((assignments.get(row["symbol"], {}).get("primary_theme") for row in group
                            if isinstance(assignments.get(row["symbol"]), Mapping)), None)
            theme_stats[key] = {"theme": primary.get("name") if isinstance(primary, Mapping) else key,
                                **_stats(group)}
        checkpoints[checkpoint_label] = {
            "provisional": checkpoint_label == "0925", "rows": rows,
            "matrix": {"by_level": level_stats, "by_theme": theme_stats,
                       "overall": _stats(rows, with_return=True)},
        }
    payload = {"trade_date": trade_date, "used_date": used_date, "version": version,
               "stage": stage, "pool_count": len(normalized_pool), "checkpoints": checkpoints}
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload["content_version"] = "sha256-" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]
    return payload


def persist_streak_feedback(root: str | Path, product: Mapping[str, Any]) -> list[str]:
    """Append immutable per-checkpoint products; never replace an existing version."""
    base = Path(root) / "report_products" / "streak_feedback" / f"trade_date={product['trade_date']}"
    written: list[str] = []
    for checkpoint, value in product.get("checkpoints", {}).items():
        version = product.get("content_version") or "unknown"
        target = base / f"checkpoint={checkpoint}" / f"{version}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            record = {key: product.get(key) for key in ("trade_date", "used_date", "version", "content_version", "stage")}
            record.update({"checkpoint": checkpoint, **dict(value)})
            target.write_text(json.dumps(record, ensure_ascii=False, allow_nan=False,
                                          sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        written.append(str(target))
    return written
