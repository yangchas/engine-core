"""Bounded, read-only capability probe for the legacy WenCai fetcher."""

import argparse
import asyncio
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


QUERY_SUITES = {
    "smoke": [
        {"id": "smoke_limitup_streak", "query": "涨停;连板天数;去除st", "loop": True, "max_stocks": 500, "purpose": "legacy limit-up and streak query"},
        {"id": "smoke_yesterday_limitup", "query": "昨日曾涨停;昨日未涨停;去除st", "loop": False, "max_stocks": 100, "purpose": "yesterday limit-up pool"},
        {"id": "smoke_yesterday_amount_top50", "query": "昨日成交额排行前50;去除st", "loop": False, "max_stocks": 100, "purpose": "yesterday amount ranking"},
    ],
    "hot": [
        {"id": "hot_heat_top50", "query": "个股热度排名前50", "loop": False, "max_stocks": 100, "purpose": "stock heat ranking"},
        {"id": "hot_ths_top100", "query": "同花顺热门股票前100;去除st", "loop": False, "max_stocks": 150, "purpose": "Tonghuashun hot stocks"},
        {"id": "hot_popularity_top50", "query": "今日人气排行榜前50", "loop": False, "max_stocks": 100, "purpose": "today popularity ranking"},
    ],
    "auction": [
        {"id": "auction_fields", "query": "今日集合竞价涨幅;集合竞价成交额;集合竞价量比;去除st", "loop": True, "max_stocks": 1000, "purpose": "auction fields"},
        {"id": "auction_pct_gt5", "query": "{date}集合竞价涨幅大于5%", "loop": False, "max_stocks": 500, "purpose": "dated auction gain filter"},
        {"id": "auction_amount_top100", "query": "今日集合竞价金额排行前100", "loop": False, "max_stocks": 150, "purpose": "auction amount ranking"},
        {"id": "auction_limitup_925", "query": "9点25分竞价涨停", "loop": False, "max_stocks": 500, "purpose": "auction limit-up query"},
    ],
    "newstock": [
        {"id": "newstock_days_lt30", "query": "上市天数小于30", "loop": False, "max_stocks": 500, "purpose": "listed fewer than 30 days"},
        {"id": "newstock_days_lt60", "query": "次新股;上市天数小于60;去除st", "loop": False, "max_stocks": 500, "purpose": "listed fewer than 60 days"},
        {"id": "newstock_recent", "query": "近期上市新股", "loop": False, "max_stocks": 500, "purpose": "recently listed stocks"},
        {"id": "newstock_today", "query": "今日上市新股", "loop": False, "max_stocks": 500, "purpose": "listed today"},
    ],
    "history": [
        {"id": "history_limitup_streak", "query": "{date}涨停;连板天数;去除st", "loop": True, "max_stocks": 500, "purpose": "dated limit-up and streak"},
        {"id": "history_was_limitup", "query": "{date}曾涨停", "loop": False, "max_stocks": 500, "purpose": "dated previous limit-up"},
        {"id": "history_limitup_reason", "query": "{date}涨停原因", "loop": False, "max_stocks": 500, "purpose": "dated limit-up reason"},
    ],
    "boundary": [
        {"id": "boundary_loop_false", "query": "涨停", "loop": False, "max_stocks": 100, "purpose": "loop comparison false"},
        {"id": "boundary_loop_true", "query": "涨停", "loop": True, "max_stocks": 100, "purpose": "loop comparison true"},
        {"id": "boundary_max1000", "query": "今日涨幅排行", "loop": False, "max_stocks": 1000, "purpose": "max_stocks cap behavior"},
        {"id": "boundary_empty_query", "query": "", "loop": False, "max_stocks": 100, "purpose": "empty query behavior"},
        {"id": "boundary_long_query", "query": "涨停" * 2000, "loop": False, "max_stocks": 100, "purpose": "overlong query behavior"},
        {"id": "boundary_unknown_condition", "query": "明日涨停", "loop": False, "max_stocks": 100, "purpose": "unknown condition behavior"},
    ],
}


def hash_codes(codes):
    payload = "\n".join(sorted(str(code) for code in codes)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest() if codes else ""


def _cookie_string(fetcher):
    value = getattr(fetcher, "wencai_cookie", "")
    return value if isinstance(value, str) else ""


def _redaction_values(fetcher):
    method = getattr(fetcher, "redaction_values", None)
    return method() if callable(method) else _cookie_string(fetcher)


def _is_cookie_echo(value, cookie):
    text = str(value)
    if isinstance(cookie, (set, frozenset)):
        return len(text) >= 8 and text in cookie
    return len(text) >= 8 and bool(cookie) and text in cookie


def sanitize_frame(frame, cookie):
    """Drop any row containing a value that echoes a cookie substring."""
    if not isinstance(frame, pd.DataFrame):
        frame = pd.DataFrame(frame)
    keep = []
    redacted = 0
    for _, row in frame.iterrows():
        if any(_is_cookie_echo(value, cookie) for value in row.tolist() if value is not None):
            redacted += 1
        else:
            keep.append(row)
    if not keep:
        return frame.iloc[0:0].copy(), redacted
    return pd.DataFrame(keep, columns=frame.columns).reset_index(drop=True), redacted


def _plain(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(value, "item"):
        try:
            value = value.item()
        except (ValueError, AttributeError):
            pass
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _code_column(columns):
    candidates = {"代码", "股票代码", "证券代码", "code", "symbol", "stock_code"}
    return next((column for column in columns if str(column).lower() in candidates), None)


def run_suite(fetcher, queries, *, sleep_s=4, max_queries=40):
    started_at = datetime.now(timezone.utc).isoformat()
    selected = list(queries[:max_queries])
    results = []
    raw = {}
    failure_streak = 0
    aborted = False
    cookie = _redaction_values(fetcher)
    for index, query_spec in enumerate(selected):
        started = time.monotonic()
        entry = {
            "id": query_spec["id"], "query": query_spec["query"], "ok": False,
            "error_type": None, "latency_s": 0.0, "row_count": 0,
            "columns": [], "code_column_present": False, "sample_head": [],
            "result_hash": "", "redacted_rows": 0,
        }
        try:
            if callable(fetcher):
                frame = fetcher(
                    query_spec["query"], query_spec["loop"], query_spec["max_stocks"],
                )
            else:
                frame = asyncio.run(fetcher._get_wencai_stocks(
                    query_spec["query"], loop=query_spec["loop"],
                    max_stocks=query_spec["max_stocks"], return_df=True,
                ))
            if not isinstance(frame, pd.DataFrame):
                frame = pd.DataFrame(frame)
            safe_frame, redacted = sanitize_frame(frame, cookie)
            code_col = _code_column(frame.columns)
            entry.update({
                "ok": True, "row_count": int(len(frame)),
                "columns": [str(column) for column in frame.columns],
                "code_column_present": code_col is not None,
                "redacted_rows": redacted,
                "sample_head": [[_plain(value) for value in row] for row in safe_frame.head(3).itertuples(index=False, name=None)],
            })
            codes = safe_frame[code_col].tolist() if code_col is not None else []
            entry["result_hash"] = hash_codes(codes)
            split = safe_frame.to_dict(orient="split")
            raw[query_spec["id"]] = {
                "index": [_plain(value) for value in split["index"]],
                "columns": [str(value) for value in split["columns"]],
                "data": [[_plain(value) for value in row] for row in split["data"]],
            }
            failure_streak = 0
        except Exception as exc:
            entry["error_type"] = type(exc).__name__
            failure_streak += 1
        entry["latency_s"] = round(time.monotonic() - started, 6)
        results.append(entry)
        if failure_streak >= 5:
            aborted = True
            break
        if sleep_s and index < len(selected) - 1:
            time.sleep(sleep_s)
    return {
        "results": results,
        "raw": raw,
        "manifest": {
            "started_at": started_at,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "query_count": len(results),
            "failure_count": sum(not row["ok"] for row in results),
            "sleep_s": sleep_s,
            "truncated": len(queries) > len(selected),
            "aborted": aborted,
        },
    }


def validate_output_dir(path):
    path = Path(path)
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise ValueError("output directory must be nonexistent or empty")
    return path


def _render_markdown(report):
    lines = ["# WenCai probe results", "", "| id | ok | rows | columns | latency_s | error_type |", "|---|---:|---:|---|---:|---|"]
    for row in report["results"]:
        lines.append("| {id} | {ok} | {count} | {columns} | {latency} | {error} |".format(
            id=row["id"], ok=row["ok"], count=row["row_count"],
            columns=", ".join(row["columns"]).replace("|", "\\|"),
            latency=row["latency_s"], error=row["error_type"] or "",
        ))
    return "\n".join(lines) + "\n"


def _queries_for_suite(suite, date):
    names = list(QUERY_SUITES) if suite == "all" else [suite]
    queries = []
    for name in names:
        for query in QUERY_SUITES[name]:
            item = dict(query)
            item["query"] = item["query"].replace("{date}", date)
            queries.append(item)
    return queries


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-root", default="/home/exedev/repos/stock-situation-runtime")
    parser.add_argument("--suite", choices=["smoke", "hot", "auction", "newstock", "history", "boundary", "all"], default="smoke")
    parser.add_argument("--sleep-s", type=float, default=4)
    parser.add_argument("--max-queries", type=int, default=40)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--date", default="")
    args = parser.parse_args(argv)
    if args.sleep_s < 0 or args.max_queries < 1:
        parser.error("--sleep-s must be >= 0 and --max-queries must be >= 1")
    out_dir = validate_output_dir(args.output_dir)
    from examples.legacy_adapters import wencai_fetch_callable

    fetcher = wencai_fetch_callable(args.legacy_root)
    queries = _queries_for_suite(args.suite, args.date)
    report = run_suite(fetcher, queries, sleep_s=args.sleep_s, max_queries=args.max_queries)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "raw").mkdir()
    (out_dir / "results.json").write_text(json.dumps(report["results"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out_dir / "results.md").write_text(_render_markdown(report), encoding="utf-8")
    (out_dir / "manifest.json").write_text(json.dumps({"parameters": vars(args), **report["manifest"]}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for query_id, payload in report["raw"].items():
        (out_dir / "raw" / f"{query_id}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"queries={report['manifest']['query_count']} failures={report['manifest']['failure_count']} aborted={report['manifest']['aborted']}")


if __name__ == "__main__":
    main()
