"""Fetch a WenCai result set through adaptive market-cap ranges."""

import argparse
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class SegmentedResult:
    rows: list[dict]
    segments: list[dict]
    complete: bool
    uncapped_cover_note: str
    duplicate_count: int = 0
    boundary_duplicates: int = 0


def _records(frame):
    records = frame.to_dict(orient="records")
    return records if isinstance(records, list) else []


def _code_column(row):
    candidates = {"股票代码", "代码", "证券代码", "code", "symbol", "stock_code"}
    return next((key for key in row if str(key).lower() in candidates), None)


def _code_key(row):
    column = _code_column(row)
    if column is None or row.get(column) is None:
        return None
    value = str(row[column]).strip()
    if value.endswith(".0"):
        value = value[:-2]
    return value[:6] if value else None


def _segment_query(base_query, lo_yi, hi_yi):
    parts = [base_query.strip().rstrip(";")]
    if lo_yi is not None and lo_yi > 0:
        parts.append(f"流通市值大于{lo_yi:g}亿")
    if hi_yi is not None:
        parts.append(f"流通市值小于{hi_yi:g}亿")
    return ";".join(part for part in parts if part)


def _width(lo, hi):
    if lo is None or hi is None:
        return None
    return hi - lo


def fetch_segmented(
    fetch, base_query, *, loop=True, cap=1000,
    edges_yi=(0, 10, 20, 30, 40, 50, 80, 120, 200, 500, None),
    min_width_yi=1.0, max_queries=80, sleep_s=4,
):
    """Fetch adjacent [lo, hi) intervals, splitting capped ranges as needed.

    ``fetch`` receives ``(query, loop, max_stocks)`` and returns a
    DataFrame-like value supporting ``len`` and ``to_dict('records')``.
    """
    edges = tuple(edges_yi)
    if len(edges) < 2 or edges[0] != 0 or any(
        edge is not None and (not isinstance(edge, (int, float)) or edge < 0)
        for edge in edges
    ):
        raise ValueError("edges_yi must start at 0 and contain nonnegative numbers")
    finite = [edge for edge in edges if edge is not None]
    if finite != sorted(set(finite)) or (None in edges and edges[-1] is not None):
        raise ValueError("edges_yi must be strictly increasing with None only last")
    if cap < 1 or max_queries < 1 or min_width_yi < 0:
        raise ValueError("cap/max_queries must be positive and min_width_yi nonnegative")

    pending = [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]
    segments = []
    rows = []
    seen = set()
    duplicate_count = 0
    boundary_duplicates = 0
    query_count = 0
    failure_streak = 0
    complete = True
    unresolved = []

    while pending:
        lo, hi = pending.pop(0)
        if query_count >= max_queries:
            complete = False
            pending.insert(0, (lo, hi))
            break

        query = _segment_query(base_query, lo, hi)
        requested_cap = cap if loop else 100
        threshold = max(1, cap - 1) if loop else 100
        started = time.monotonic()
        segment_rows = None
        error_type = None
        attempts = 0
        while attempts < 2 and query_count < max_queries:
            if attempts and sleep_s:
                time.sleep(sleep_s)
            attempts += 1
            query_count += 1
            try:
                frame = fetch(query, loop, requested_cap)
                segment_rows = _records(frame)
                break
            except Exception as exc:
                error_type = type(exc).__name__
        latency = round(time.monotonic() - started, 6)
        if segment_rows is None:
            segments.append({
                "lo_yi": lo, "hi_yi": hi, "query": query, "row_count": 0,
                "capped": False, "latency_s": latency, "ok": False,
                "error_type": error_type,
            })
            complete = False
            failure_streak += 1
            if failure_streak >= 4:
                complete = False
                break
            continue

        failure_streak = 0
        row_count = len(segment_rows)
        capped = row_count >= threshold
        segments.append({
            "lo_yi": lo, "hi_yi": hi, "query": query, "row_count": row_count,
            "capped": capped, "latency_s": latency, "ok": True,
            "error_type": None,
        })

        width = _width(lo, hi)
        split_range = capped and width is not None and width > min_width_yi
        if capped:
            if split_range:
                midpoint = (lo + hi) / 2
                if midpoint <= lo or midpoint >= hi:
                    unresolved.append((lo, hi))
                    complete = False
                    split_range = False
                else:
                    pending[0:0] = [(lo, midpoint), (midpoint, hi)]
            else:
                unresolved.append((lo, hi))
                complete = False

        # A capped parent is only a probe: its child intervals replace it.
        if not split_range:
            segment_seen = set()
            for row in segment_rows:
                key = _code_key(row)
                if key is not None and key in seen:
                    duplicate_count += 1
                    if key not in segment_seen:
                        boundary_duplicates += 1
                    continue
                if key is not None:
                    seen.add(key)
                    segment_seen.add(key)
                rows.append(row)

        if sleep_s and pending and query_count < max_queries:
            time.sleep(sleep_s)

    if pending:
        complete = False
    if any(not segment["ok"] for segment in segments):
        complete = False
    note = "all queried ranges below cap"
    if unresolved:
        note = "capped_unresolved=" + ",".join(f"[{lo},{hi})" for lo, hi in unresolved)
    elif pending:
        note = f"query_limit_reached={max_queries}; ranges_remaining={len(pending)}"
    elif any(not segment["ok"] for segment in segments):
        note = "one or more ranges failed after retry"
    return SegmentedResult(rows, segments, complete, note, duplicate_count, boundary_duplicates)


def _parse_edges(value):
    result = []
    for item in value.split(","):
        item = item.strip().lower()
        result.append(None if item in {"", "none"} else float(item))
    return tuple(result)


def validate_output_dir(path):
    path = Path(path)
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise ValueError("output directory must be nonexistent or empty")
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", required=True, help="base WenCai query")
    parser.add_argument("--edges", default="0,10,20,30,50,80,120,200,500,None")
    parser.add_argument("--cap", type=int, default=1000)
    parser.add_argument("--loop", dest="loop", action="store_true", default=True)
    parser.add_argument("--no-loop", dest="loop", action="store_false")
    parser.add_argument("--sleep-s", type=float, default=4)
    parser.add_argument("--max-queries", type=int, default=80)
    parser.add_argument("--legacy-root", default="/home/exedev/repos/stock-situation-runtime")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    if args.sleep_s < 0 or args.cap < 1 or args.max_queries < 1:
        parser.error("--sleep-s must be >= 0; --cap and --max-queries must be >= 1")
    try:
        edges = _parse_edges(args.edges)
        out_dir = validate_output_dir(args.output_dir)
    except ValueError as exc:
        parser.error(str(exc))

    from examples.legacy_adapters import wencai_fetch_callable

    fetch = wencai_fetch_callable(args.legacy_root)

    result = fetch_segmented(
        fetch, args.query, loop=args.loop, cap=args.cap, edges_yi=edges,
        max_queries=args.max_queries, sleep_s=args.sleep_s,
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "rows.json").write_text(json.dumps(result.rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out_dir / "segments.json").write_text(json.dumps(result.segments, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = asdict(result)
    manifest.pop("rows")
    manifest.pop("segments")
    manifest.update({"base_query": args.query, "loop": args.loop, "cap": args.cap, "edges_yi": edges})
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"segments={len(result.segments)} rows={len(result.rows)} complete={result.complete} duplicates={result.duplicate_count}")


if __name__ == "__main__":
    main()
