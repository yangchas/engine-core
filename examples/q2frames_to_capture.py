"""Convert a Q2Frame stream into a sealed Redis Q2 read capture."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo


_LOCAL_TZ = ZoneInfo("Asia/Shanghai")


def _cutoff_datetime(trade_date: str, cutoff: str) -> datetime:
    day = date.fromisoformat(trade_date)
    local = datetime.fromisoformat(f"{day.isoformat()}T{cutoff}")
    if local.tzinfo is not None:
        raise ValueError("cutoff must be a local time without a timezone")
    return local.replace(tzinfo=_LOCAL_TZ)


def convert_q2frames_to_capture(
    q2frame: str | Path,
    trade_date: str,
    cutoff: str,
    output: str | Path,
) -> dict:
    """Merge frames through cutoff and write a RedisQ2ReadCaptureV1 artifact."""
    cutoff_at = _cutoff_datetime(trade_date, cutoff)
    cutoff_ms = int(cutoff_at.timestamp() * 1000)
    state: dict[str, dict] = {}
    with Path(q2frame).open("r", encoding="utf-8") as handle:
        for line in handle:
            frame = json.loads(line)
            if frame["logical_ts_ms"] > cutoff_ms:
                break
            for update in frame["q2_updates"]:
                symbol = update["symbol"]
                state.setdefault(symbol, {}).update(update)

    reads = [{
        "operation": "smembers",
        "key": f"q2:active:{trade_date.replace('-', '')}",
        "value": sorted(state),
    }]
    for symbol in sorted(state):
        reads.append({
            "operation": "hgetall",
            "key": f"q2:{symbol}",
            "value": {key: str(value) for key, value in state[symbol].items() if key != "symbol"},
        })
    capture = {
        "contract_version": "RedisQ2ReadCaptureV1",
        "trade_date": trade_date,
        "read_completed_at": cutoff_at.isoformat(timespec="seconds"),
        "reads": reads,
    }
    with Path(output).open("x", encoding="utf-8") as handle:
        json.dump(capture, handle, ensure_ascii=False, separators=(",", ":"))
        handle.write("\n")
    return capture


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--q2frame", type=Path, required=True)
    parser.add_argument("--trade-date", required=True)
    parser.add_argument("--cutoff", required=True, help="Local time, HH:MM:SS")
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    convert_q2frames_to_capture(args.q2frame, args.trade_date, args.cutoff, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
