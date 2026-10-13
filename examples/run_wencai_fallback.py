"""Run an on-demand WenCai fallback and save normalized rows plus a manifest."""

import argparse
import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable


def validate_output_dir(path: str | Path) -> Path:
    out = Path(path)
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ValueError("output directory must be empty or not exist")
    return out


def _default_fetcher(legacy_root: str) -> Callable[..., Any]:
    from examples.legacy_adapters import wencai_fetch_callable

    return wencai_fetch_callable(legacy_root)


def run(args: argparse.Namespace, fetch: Callable[..., Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    from engine_core.wencai_fallback import fetch_auction_fallback, fetch_limit_up_fallback

    started = time.monotonic()
    results: dict[str, Any] = {}
    for what in (["auction", "limit_up"] if args.what == "both" else [args.what]):
        if what == "auction":
            result = fetch_auction_fallback(
                fetch, date=args.date, min_amount_yuan=args.min_amount_wan * 10_000,
                include_st=not args.no_st, max_queries=4,
            )
        else:
            result = fetch_limit_up_fallback(fetch, date=args.date, include_st=not args.no_st)
        results[what] = asdict(result)
    manifest = {
        "what": args.what,
        "requested_date": args.date,
        "query_log": {key: value["query_log"] for key, value in results.items()},
        "labels": {key: value["labels"] for key, value in results.items()},
        "complete": {key: value["complete"] for key, value in results.items()},
        "elapsed_s": round(time.monotonic() - started, 6),
    }
    return results, manifest


def main(argv: list[str] | None = None, *, fetch: Callable[..., Any] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--what", choices=("auction", "limit_up", "both"), default="both")
    parser.add_argument("--date", help="YYYY-MM-DD; omitted means today for auction")
    parser.add_argument("--min-amount-wan", type=int, default=500)
    parser.add_argument("--no-st", action="store_true")
    parser.add_argument("--legacy-root", default="/home/exedev/repos/stock-situation-runtime")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    if args.min_amount_wan < 0:
        parser.error("--min-amount-wan must be >= 0")
    try:
        out = validate_output_dir(args.output_dir)
    except ValueError as exc:
        parser.error(str(exc))
    client_fetch = fetch or _default_fetcher(args.legacy_root)
    results, manifest = run(args, client_fetch)
    out.mkdir(parents=True, exist_ok=True)
    (out / "result.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"what={args.what} complete={all(manifest['complete'].values())} labels={manifest['labels']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
