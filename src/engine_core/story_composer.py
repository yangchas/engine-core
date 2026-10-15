"""Build restrained four-act text from already-scoped structured facts."""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

_ACTS = ("auction", "open", "intraday", "close")
_LABEL = {"auction": "竞价", "open": "开盘", "intraday": "盘中", "close": "收盘"}


def _default_templates() -> list[dict[str, Any]]:
    """Load the checked-in C201 library; an absent optional example file is no match."""
    path = Path(__file__).resolve().parents[2] / "examples" / "data" / "story_templates.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    templates = value.get("templates", []) if isinstance(value, Mapping) else []
    return [row for row in templates if isinstance(row, dict)]


def _claim(claim_id: str, text: str, refs: Sequence[Any] = ()) -> dict[str, Any]:
    return {"claim_id": claim_id, "text": text, "refs": [str(x) for x in refs]}


def _fmt(value: Any, unit: Any) -> str:
    if value is None: return "缺失"
    if unit == "ratio": return f"{float(value) * 100:.1f}%"
    if unit == "yuan": return f"{float(value) / 1e8:.1f}亿元"
    if unit == "pp": return f"{float(value):+.2f}个百分点"
    return str(value)


def _metric_label(key: str) -> str:
    return {"auction_median_pct": "竞价中位", "auction_up_share": "上涨占比",
            "auction_amount_yuan": "竞价匹配额", "auction_prev_common_ratio": "竞价额/前日同池",
            "open_median_pct": "开盘中位", "open_up_share": "上涨占比",
            "open_amount_yuan": "开盘成交额", "p0945_median": "0945中位",
            "p0945_up_share": "上涨占比", "p0945_amount_yuan": "0945累计成交额",
            "close_median_pct": "收盘中位", "close_up_share": "上涨占比",
            "close_amount_yuan": "收盘成交额", "close_vs_p0945_pp": "收盘较0945"}.get(key, key)


def _metric_unit(key: str, unit: Any) -> Any:
    if unit is not None:
        return unit
    if key.endswith("_share") or key.endswith("_ratio"):
        return "ratio"
    if key.endswith("_amount_yuan"):
        return "yuan"
    if key.endswith("_pct") or key.endswith("_pp") or key == "p0945_median":
        return "pp"
    return None


def _condition_matches(node: Any, metrics: Mapping[str, Any]) -> bool:
    if not isinstance(node, Mapping):
        return False
    if "all" in node:
        return all(_condition_matches(child, metrics) for child in node["all"])
    if "any" in node:
        return any(_condition_matches(child, metrics) for child in node["any"])
    field, op, expected = node.get("field"), node.get("op"), node.get("value")
    item = metrics.get(str(field))
    actual = item.get("value") if isinstance(item, Mapping) else None
    if actual is None:
        return False
    try:
        if op == "eq": return actual == expected
        if op == "in": return actual in expected
        if op == "gte": return actual >= expected
        if op == "lte": return actual <= expected
        if op == "gt": return actual > expected
        if op == "lt": return actual < expected
        if op == "between_closed": return expected[0] <= actual <= expected[1]
        if op == "abs_lte": return abs(actual) <= expected
    except (TypeError, ValueError, IndexError):
        return False
    return False


def compose_story(features: Mapping[str, Any], anomalies: Sequence[Mapping[str, Any]],
                  funnel: Mapping[str, Any], *, previous_acts: Sequence[Mapping[str, Any]],
                  templates: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    stage, date = str(features.get("stage", "auction")), str(features.get("trade_date", ""))
    metrics = features.get("metrics", {}) if isinstance(features.get("metrics"), Mapping) else {}
    refs = [str(x) for x in features.get("source_refs", [])] if isinstance(features.get("source_refs"), Sequence) else []
    events = []
    for anomaly in anomalies:
        if anomaly.get("display_eligible") and len(events) < 3:
            label = "历史异常" if anomaly.get("kind") in {"TAIL", "ROBUST"} else "对照观察"
            events.append({"id": anomaly.get("id"), "family": anomaly.get("family"),
                           "display_label": label, "value": anomaly.get("value"),
                           "quality": anomaly.get("quality", []), "ref": anomaly.get("refs", [])})
    shown = funnel.get("shown", {}) if isinstance(funnel.get("shown"), Mapping) else {}
    lines = []
    for label in ("watch", "wait", "exclude"):
        entries = shown.get(label, [])
        lines.extend(f"{label}:{row.get('name')}｜{row.get('condition', {}).get('text', '')}｜风险：{row.get('risk', '')}"
                     for row in entries[:5])
    lead_values = []
    lead_keys = {
        "auction": ("auction_median_pct", "auction_up_share", "auction_amount_yuan", "auction_prev_common_ratio"),
        "open": ("open_median_pct", "open_up_share", "open_amount_yuan"),
        "intraday": ("p0945_median", "p0945_up_share", "p0945_amount_yuan"),
        "close": ("close_median_pct", "close_up_share", "close_amount_yuan", "close_vs_p0945_pp"),
    }.get(stage, ())
    for key in lead_keys:
        metric = metrics.get(key)
        if isinstance(metric, Mapping) and metric.get("value") is not None:
            lead_values.append(f"{_metric_label(key)}{_fmt(metric['value'], _metric_unit(key, metric.get('unit')))}")
    headline_text = (f"{date} { _LABEL.get(stage, stage) }观察：" + ("；".join(lead_values[:2]) if lead_values else "当前结构化事实有限，按缺口观察。"))
    acts, earlier = [], {str(a.get("stage")): a for a in previous_acts if isinstance(a, Mapping)}
    if templates is None:
        templates = _default_templates()
    matched_templates = [t for t in templates if isinstance(t, Mapping) and stage in t.get("stages", [])
                         and _condition_matches(t.get("conditions"), metrics)]
    for act_stage in _ACTS:
        if _ACTS.index(act_stage) > _ACTS.index(stage) if stage in _ACTS else True:
            acts.append({"stage": act_stage, "status": "UNOBSERVED", "lead": _claim(f"story:{act_stage}:lead", "该阶段尚无可用观测。"),
                         "evidence": [], "transition": {"from": None, "condition_id": None,
                         "observed_result": None, "rule_id": None}, "pending_conditions": [], "template_ids": []})
            continue
        if act_stage in earlier:
            acts.append(dict(earlier[act_stage])); continue
        current = act_stage == stage
        if not current:
            acts.append({"stage": act_stage, "status": "UNOBSERVED",
                         "lead": _claim(f"story:{act_stage}:lead", "该幕独立时点事实未提供。"),
                         "evidence": [], "transition": {"from": None, "condition_id": None,
                         "observed_result": None, "rule_id": None}, "pending_conditions": [], "template_ids": []})
            continue
        relevant = [(k, metrics[k]) for k in lead_keys if k in metrics and isinstance(metrics[k], Mapping)
                    and metrics[k].get("value") is not None]
        evidence = [_claim(f"story:{act_stage}:evidence:{i}", f"{k}：{_fmt(v.get('value'), v.get('unit'))}", v.get("refs", []))
                    for i, (k, v) in enumerate(relevant[:3])]
        label = _LABEL[act_stage]
        active_templates = matched_templates
        stage_values = lead_values[:3]
        text = (str(active_templates[0].get("neutral_sentence")) if active_templates else
                f"{label}观测：" + "；".join(stage_values) + "。" if relevant else
                f"{label}阶段可用事实不足，结论保留缺口。")
        acts.append({"stage": act_stage, "status": "OBSERVED",
                     "lead": _claim(f"story:{act_stage}:lead", text, refs), "evidence": evidence,
                     "transition": {"from": None, "condition_id": None, "observed_result": None,
                                    "rule_id": None}, "pending_conditions": [],
                     "template_ids": [str(x.get("id")) for x in active_templates]})
    top = []
    for ev in events:
        top.append({"id": ev["id"], "display_label": ev["display_label"], "family": ev["family"],
                    "value": ev["value"], "ref": ev["ref"], "quality": ",".join(ev["quality"])})
    return {"schema": "StoryV1", "trade_date": date, "stage": stage,
            "generated_at": features.get("observed_at"), "headline": _claim("story:headline", headline_text, refs),
            "top_events": top[:3], "acts": acts[:4], "funnel": {k: shown.get(k, [])[:5] for k in ("watch", "wait", "exclude")},
            "quality": list(features.get("quality", [])), "refs": refs,
            "template_status": "DESCRIPTIVE_ONLY"}
