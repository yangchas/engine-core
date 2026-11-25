"""A cautious, deterministic A→B observation funnel (not a trade instruction)."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from typing import Any

_BUCKET = {"能做": "watch", "要等": "wait", "别碰": "exclude", "watch": "watch", "wait": "wait", "exclude": "exclude"}
_LABEL = {"watch": "能做", "wait": "要等", "exclude": "别碰"}


def _number(source: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        try:
            value = source.get(key)
            if value not in (None, ""):
                return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _candidate_language(source: Mapping[str, Any], bucket: str, status: str,
                        position: Any, relative: float | None, stage: str,
                        checkpoint: str) -> tuple[str, str]:
    reason = str(source.get("reason") or source.get("reasons") or "").strip()
    if isinstance(source.get("reasons"), Sequence) and not isinstance(source.get("reasons"), str):
        reason = "；".join(str(x) for x in source["reasons"] if x)
    condition = str(source.get("condition") or "").strip()
    risk = str(source.get("risk") or "").strip()
    position_value = _number(source, "position_asof", "position20_prior", "position")
    own_move = (_number(source, "auction_pct", "p0925_pct") if stage == "auction" else
                _number(source, f"p{checkpoint}_pct", "current_pct", "intraday_pct"))
    auction_move = _number(source, "auction_pct", "p0925_pct")
    plate_move = _number(source, f"plate{checkpoint}", "plate_current_pct", "plate_pct")
    if condition in {"等待信号确认", "风险未明", "等待信号", "先确认后观察"}:
        condition = ""
    if not condition:
        if status in {"INDICATIVE", "PRICE_PROXY"}:
            reference_move = auction_move if auction_move is not None else own_move
            if reference_move is not None and stage == "auction":
                floor = 5.0 if reference_move >= 5.0 else 0.0
                condition = (f"竞价指示{reference_move:+.1f}%，终价确认后看0932仍≥{floor:.0f}%且不弱于板块"
                             if floor else f"竞价指示{reference_move:+.1f}%，终价确认后看0932保持红盘且不弱于板块")
            elif reference_move is not None and stage == "open":
                condition = f"竞价指示{reference_move:+.1f}%，{checkpoint}{own_move:+.1f}%，看后续保持红盘且不弱于板块"
            elif reference_move is not None:
                condition = f"参考涨幅{reference_move:+.1f}%，终价确认后再复核"
        elif bucket == "exclude" and position_value is not None and position_value >= 0.67:
            condition = "下一交易日重算20日位置；仍处高位则不纳入观察池"
        elif stage == "auction" and auction_move is not None:
            floor = 5.0 if auction_move >= 5.0 else 0.0
            condition = (f"竞价{auction_move:+.1f}%，看0932仍≥{floor:.0f}%且相对板块不转弱"
                         if floor else f"竞价{auction_move:+.1f}%，看0932保持红盘且不弱于板块")
        elif stage in {"open", "close"} and own_move is not None:
            condition = (f"竞价{auction_move:+.1f}%后{checkpoint}{own_move:+.1f}%，看后续保持红盘且不弱于板块"
                         if auction_move is not None else f"{checkpoint}{own_move:+.1f}%，看后续保持红盘且不弱于板块")
        elif relative is not None and relative < -0.5:
            condition = f"个股落后所属板块{abs(relative):.1f}个百分点；重回开盘价并缩小差距后复核"
        elif relative is not None and relative > 0.5:
            condition = f"个股暂强于所属板块{relative:.1f}个百分点；观察相对强势能否延续"
        elif own_move is not None and own_move < -1:
            condition = f"仍低于昨收{abs(own_move):.1f}%；先站回开盘价再复核"
        else:
            condition = ""
    if not reason:
        if status in {"INDICATIVE", "PRICE_PROXY"}:
            reason = "竞价价格仍是指示价，不能当作最终开盘价"
        elif position_value is not None and position_value >= 0.8:
            reason = f"昨日收盘位于20日区间约{position_value * 100:.0f}%位置"
        elif relative is not None and relative < -0.5:
            reason = f"早盘落后所属板块{abs(relative):.1f}个百分点"
        elif relative is not None and relative > 0.5:
            reason = f"早盘跑赢所属板块{relative:.1f}个百分点"
        elif own_move is not None:
            prefix = checkpoint if stage in {"open", "close"} else "竞价"
            reason = f"{prefix}个股涨跌{own_move:+.1f}%，板块约{plate_move:+.1f}%" if plate_move is not None else f"{prefix}个股涨跌{own_move:+.1f}%"
        else:
            reason = "当前候选事实不足，保留观察"
    if not risk:
        if status in {"INDICATIVE", "PRICE_PROXY"}:
            reference_move = auction_move if auction_move is not None else own_move
            risk = f"竞价参考{reference_move:+.1f}%尚非终价" if reference_move is not None else ""
        elif position_value is not None and position_value >= 0.8:
            risk = "高位回撤"
        elif relative is not None and relative < -0.5:
            risk = "板块相对偏弱"
        else:
            risk = f"需复核自身涨幅{own_move:+.1f}%" if own_move is not None else ""
    elif not re.search(r"\d+(?:\.\d+)?", risk):
        risk_move = own_move if own_move is not None else relative
        if risk_move is not None:
            unit = "%" if own_move is not None else "个百分点"
            risk = f"{risk}（个股{risk_move:+.1f}{unit}）"
    return condition, risk


def filter_candidates(candidates: Sequence[Mapping[str, Any]], features: Mapping[str, Any], *,
                      asof: str, policy: Mapping[str, Any] | None = None,
                      checkpoint: str | None = None) -> dict[str, Any]:
    stage = str(features.get("stage", "auction"))
    if stage == "open":
        if checkpoint is None:
            checkpoint = str(features.get("checkpoint") or "")
        if not checkpoint:
            from .live_session import open_checkpoint_for_asof
            checkpoint = open_checkpoint_for_asof(asof)
    elif stage == "close":
        checkpoint = "0945"
    else:
        checkpoint = "0925"
    metrics = features.get("metrics", {}) if isinstance(features.get("metrics"), Mapping) else {}
    rows, counts = {k: [] for k in _LABEL}, {k: 0 for k in _LABEL}
    seen: dict[str, dict[str, Any]] = {}
    for i, source in enumerate(candidates):
        bucket = _BUCKET.get(str(source.get("bucket", source.get("classification", source.get("status", "wait")))) , "wait")
        symbol = str(source.get("symbol", source.get("a_symbol", "")))
        name = str(source.get("name", source.get("a_name", symbol or "未命名")))
        # Explicitly ignore all later-stage result fields in early-stage eligibility and ordering.
        has_price_status = "price_status" in source or "auction_price_status" in source
        status = str(source.get("price_status", source.get("auction_price_status", "UNSPECIFIED")))
        position = source.get("position_asof", source.get("position20_prior"))
        if source.get("auction_price_status"):
            status = str(source["auction_price_status"])
        required_price_keys = (f"p{checkpoint}_pct", "current_pct", "intraday_pct") if stage == "open" else ("auction_pct", "p0925_pct")
        price_field_present = any(key in source for key in required_price_keys)
        required_price = _number(source, *required_price_keys)
        if (symbol.strip().lower() in {"", "none", "null", "unknown"}
                or has_price_status and status.upper() in {"UNKNOWN", "MISSING", "UNAVAILABLE"}
                or price_field_present and required_price is None):
            continue
        # Keep why the candidate was selected separate from why it has not
        # cleared the current observation filter. Ranking text is never a
        # rejection reason.
        reasons_raw = source.get("selection_reasons", source.get("selection_reason", []))
        if not reasons_raw:
            reasons_raw = source.get("reasons", [])
        if isinstance(reasons_raw, str): reasons_raw = [reasons_raw]
        reasons = [str(x) for x in reasons_raw] if isinstance(reasons_raw, Sequence) else []
        _seen_rank = False
        _dedup = []
        for _r in reasons:  # one rank statement only; B can be listed under several A with different local ranks
            if _r.startswith("板块竞价额第"):
                if _seen_rank:
                    continue
                _seen_rank = True
            _dedup.append(_r)
        reasons = _dedup
        filter_reasons = source.get("filter_reasons", [])
        if isinstance(filter_reasons, str): filter_reasons = [filter_reasons]
        filter_reasons = [str(x) for x in filter_reasons if x]
        if not filter_reasons:
            amount = _number(source, "match_amount_yuan", "auction_amount_yuan")
            unlimited = bool(source.get("is_unlimited_price_band") or source.get("price_band_label") == "新股/无涨跌幅限制")
            if status in {"INDICATIVE", "PRICE_PROXY"}:
                filter_reasons.append("竞价价仍为指示价，终价未确认")
            elif unlimited:
                filter_reasons.append("新股无涨跌幅限制，竞价位置不可按涨跌停判断")
            elif status in {"LIMIT_UP", "AT_LIMIT_UP", "SEALED_UP"} or source.get("auction_limit_state") in (1, "1", "UP", "LIMIT_UP"):
                filter_reasons.append("竞价已在涨停价，无法按可买入候选处理")
            elif amount is not None and amount < 10_000_000:
                filter_reasons.append("竞价金额过小")
            elif position is not None and _number(source, "position_asof", "position20_prior", "position") is not None and _number(source, "position_asof", "position20_prior", "position") >= .8:
                filter_reasons.append("昨日位置偏高")
            elif source.get("plate_qualified") is False:
                filter_reasons.append("所属板块不满足观察范围")
            elif status in {"UNKNOWN", "MISSING", "UNAVAILABLE"}:
                filter_reasons.append("价格或成交数据缺失")
            elif status == "UNSPECIFIED":
                filter_reasons.append("价格或成交数据缺失")
            else:
                filter_reasons.append("当前截点条件尚未满足")
        relative = (_number(source, f"relative_to_plate_{checkpoint}_pp", f"relative{checkpoint}",
                            "relative_pct", "relative_to_plate_pp"))
        condition, risk = _candidate_language(source, bucket, status, position, relative, stage, checkpoint)
        if not condition or not risk:
            continue
        if not reasons:
            reasons = ["板内竞价金额与相对表现值得继续观察"]
        if position is None and "position" in source:
            position = source.get("position")
        ref = source.get("origin_refs", source.get("refs", []))
        item = {"symbol": symbol, "name": name, "bucket": bucket,
            "reasons": [{"claim_id": f"candidate:{symbol}:{j}", "text": reason, "refs": list(ref) if isinstance(ref, Sequence) and not isinstance(ref, str) else []}
                        for j, reason in enumerate(reasons[:3])],
            "condition": {"claim_id": f"candidate:{symbol}:condition", "text": condition,
                          "refs": list(ref) if isinstance(ref, Sequence) and not isinstance(ref, str) else []},
            "risk": risk, "origin_refs": list(ref) if isinstance(ref, Sequence) and not isinstance(ref, str) else [],
            "filter_reasons": filter_reasons,
            "price_status": status, "position_asof": source.get("position_asof"), "asof": asof,
            "origin": str(source.get("origin", "A_TO_B")), "_index": i}
        if symbol and symbol in seen:
            # Same stock reached through multiple themes: one display row, with
            # the more cautious bucket winning and reasons kept as references.
            previous = seen[symbol]
            order = {"watch": 0, "wait": 1, "exclude": 2}
            if order[bucket] > order[previous["bucket"]]:
                rows[previous["bucket"]].remove(previous)
                rows[bucket].append(item)
                seen[symbol] = item
            else:
                previous["reasons"] = (previous["reasons"] + item["reasons"])[:3]
                previous["origin_refs"] = list(dict.fromkeys(previous["origin_refs"] + item["origin_refs"]))
        else:
            rows[bucket].append(item)
            if symbol:
                seen[symbol] = item
    counts = {bucket: len(rows[bucket]) for bucket in _LABEL}
    reason_counts: dict[str, int] = {}
    for bucket_rows in rows.values():
        for row in bucket_rows:
            for label in row["filter_reasons"]:
                reason_counts[label] = reason_counts.get(label, 0) + 1
    shown = {}
    for bucket, label in _LABEL.items():
        # Preserve source order; never rank on later outcome fields or synthesize a score.
        shown[bucket] = [{k: v for k, v in row.items() if k != "_index"} for row in rows[bucket][:5]]
    return {"asof": asof, "candidate_origin": "A_TO_B", "counts": counts, "shown": shown,
            "reason_counts": reason_counts, "all_rows_ref": None, "stage": stage}
