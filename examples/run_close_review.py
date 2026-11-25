"""Write the compact descriptive auction-to-close review artifacts."""
from __future__ import annotations

import sys
import argparse
import csv
import json
from pathlib import Path

from engine_core.close_review import main
from engine_core.story_features import build_story_features
from engine_core.tradability_funnel import filter_candidates
from engine_core.story_composer import compose_story


def _fixture(directory: Path, output: Path) -> int:
    facts = json.loads((directory / "run02/narrative_facts.json").read_text(encoding="utf-8"))
    with (directory / "run02/candidate_funnel_20261015.csv").open(encoding="utf-8", newline="") as stream:
        candidates = list(csv.DictReader(stream))
    aliases = {"能做": "watch", "要等": "wait", "别碰": "exclude"}
    for row in candidates:
        row["bucket"] = aliases.get(row.get("bucket", ""), "wait")
        row["reasons"] = [row.get("reason", "")]
        row["condition"] = "下一观察截点检查个股与板块是否延续"
        row["risk"] = "样本来自盘后重建；历史可得时刻未知"
        row["price_status"] = row.get("auction_price_status", "UNKNOWN")
    summary = facts.get("market_timeline", [])
    metric_values = {"auction_amount_yuan": facts.get("auction_amount_yuan"),
                     "auction_prev_common_ratio": facts.get("auction_prev_common_ratio")}
    act_features = {}
    for item in summary:
        if not isinstance(item, dict):
            continue
        tag = str(item.get("tag", item.get("stage", "")))
        stage = {"0925": "auction", "0932": "open", "0945": "intraday", "close": "close"}.get(tag, tag)
        median, share = item.get("median_pp", item.get("median_pct")), item.get("up_share")
        if stage == "auction":
            metric_values.update({"auction_median_pct": median, "auction_up_share": share})
            snapshot = {"auction_median_pct": median, "auction_up_share": share,
                        "auction_amount_yuan": facts.get("auction_amount_yuan")}
        elif stage == "open":
            snapshot = {"open_median_pct": median, "open_up_share": share}
        elif stage == "intraday":
            snapshot = {"p0945_median": median, "p0945_up_share": share}
            metric_values.update(snapshot)
        elif stage == "close":
            snapshot = {"close_median_pct": median, "close_up_share": share}
            metric_values.update(snapshot)
        else:
            continue
        stage_cutoff = {"auction": "09:25", "open": "09:32", "intraday": "09:45", "close": "15:00"}[stage]
        act_features[stage] = build_story_features({"trade_date": facts.get("trade_date"), **snapshot},
            stage=stage, cutoff=stage_cutoff, observed_at=None,
            provenance={"source_fidelity": "HISTORICAL_AVAILABILITY_UNKNOWN",
                        "refs": ["run02/narrative_facts.json"]})
    feature_rows = build_story_features({"trade_date": facts.get("trade_date"), **metric_values},
                                        stage="close", cutoff="15:00", observed_at=None,
                                        provenance={"source_fidelity": "HISTORICAL_AVAILABILITY_UNKNOWN",
                                                    "refs": ["run02/narrative_facts.json"]})
    with (directory / "run02/anomalies_20261015.csv").open(encoding="utf-8", newline="") as stream:
        raw_anomalies = list(csv.DictReader(stream))
    anomalies = []
    for row in raw_anomalies:
        if row.get("decision") not in {"LOW_TAIL", "HIGH_TAIL", "ROBUST_DEVIATION", "OBSERVED_RANK_ONLY", "SMALL_DENOMINATOR_RANK_ONLY"}:
            continue
        anomalies.append({"id": row["metric"], "metric_ids": [row["metric"]],
                          "family": row.get("family", "context"), "stage": row.get("stage", "close"),
                          "kind": "TAIL" if "TAIL" in row.get("decision", "") else "OBSERVED_RANK",
                          "quality": [row.get("quality", "")], "n": int(row.get("historical_n") or 0),
                          "value": float(row["value"]), "p": float(row["empirical_percentile"] or 0),
                          "robust_z": float(row["robust_z"]) if row.get("robust_z") else None,
                          "ranks": {"descending": int(row.get("descending_rank_including_today") or 0)},
                          "refs": [row.get("source", "C201 run02")], "display_eligible": True})
    funnel = filter_candidates(candidates, feature_rows, asof="盘后重建/可得时刻未知")
    previous_acts = []
    for earlier in ("auction", "open", "intraday"):
        if earlier not in act_features:
            continue
        act_story = compose_story(act_features[earlier], [], {"shown": {}}, previous_acts=[])
        previous_acts.extend(a for a in act_story["acts"] if a["stage"] == earlier)
    story = compose_story(feature_rows, anomalies, funnel, previous_acts=previous_acts)
    lines = [story["headline"]["text"], "", "今日三件不一样的事："]
    lines.extend(f"- {x['display_label']}：{x['id']} = {x['value']}" for x in story["top_events"])
    lines.append("四幕：")
    lines.extend(f"- {x['stage']}：{x['lead']['text']}" for x in story["acts"])
    for key, label in (("watch", "能做"), ("wait", "要等"), ("exclude", "别碰")):
        lines.append(f"{label}：")
        lines.extend(f"- {x['name']}：{x['condition']['text']}；风险：{x['risk']}" for x in story["funnel"][key])
    text = "\n".join(lines[:34]) + "\n"
    output.mkdir(parents=True, exist_ok=True)
    (output / "story.json").write_text(json.dumps(story, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")
    (output / "story_sample.md").write_text(text, encoding="utf-8")
    print(f"STORY_JSON={output / 'story.json'}")
    print(f"STORY_SAMPLE={output / 'story_sample.md'}")
    print(f"SAMPLE_LINES={len(text.splitlines())}")
    print(text, end="")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--story-fixture-dir")
    parser.add_argument("--story-out")
    known, remaining = parser.parse_known_args()
    if known.story_fixture_dir:
        raise SystemExit(_fixture(Path(known.story_fixture_dir), Path(known.story_out or "/tmp/c207-story")))
    sys.argv = [sys.argv[0], *remaining]
    raise SystemExit(main())
