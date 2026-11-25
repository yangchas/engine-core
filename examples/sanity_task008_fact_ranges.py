"""Compact range and trigger sanity checks for the authorized short windows."""

from __future__ import annotations

import json
import math
import argparse
import re
from collections import defaultdict
from statistics import median
from typing import Any, Mapping
TAGS = ("0920", "0924", "0925", "OPENING_0932")
SYMBOL = re.compile(r"\d{6}(?:\.\w+)?\Z")


def number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def flatten(value: Any, prefix: str = ""):
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield from flatten(child, f"{prefix}.{key}" if prefix else str(key))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            yield from flatten(child, f"{prefix}[{index}]")
    else:
        yield prefix, value


def symbol_facts(value: Any, path: tuple[str, ...] = ()):
    """Find symbol-keyed records anywhere in the serialized ordered result."""
    if not isinstance(value, Mapping):
        return
    for key, child in value.items():
        key = str(key)
        if SYMBOL.fullmatch(key) and isinstance(child, Mapping):
            fields = list(flatten(child))
            if any(number(item) for _, item in fields):
                yield path, key, fields
        else:
            yield from symbol_facts(child, path + (key,))


def ranges_and_flags(result: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    grouped: dict[tuple[str, str], list[Any]] = defaultdict(list)
    price_by_symbol: dict[tuple[str, str, str], float] = {}
    flags: list[str] = []
    ordered = result.get("ordered", result)
    for path, symbol, fields in symbol_facts(ordered):
        group = ".".join(path)
        tag = next((item for item in path if item in TAGS), "UNKNOWN")
        for field, value in fields:
            grouped[(group, field)].append(value)
            if number(value) and math.isfinite(float(value)) and "price" in field.lower():
                price_by_symbol[(tag, symbol, field)] = float(value)
            name = field.lower()
            label = f"{group}.{symbol}.{field}={value!r}"
            if number(value):
                finite = math.isfinite(float(value))
                if any(token in name for token in ("amount", "volume", "price", "count")) and value < 0:
                    flags.append(f"NEGATIVE {label}")
                if any(token in name for token in ("pct", "change", "ratio")) and (not finite or abs(value) > 30):
                    flags.append(f"PCT_RANGE {label}")
                if "price_milli" in name and (value <= 0 or value > 2_000_000):
                    flags.append(f"PRICE_MILLI_RANGE {label}")
    lines: list[str] = []
    for (group, field), values in sorted(grouped.items()):
        nums = [float(value) for value in values if number(value) and math.isfinite(float(value))]
        if not nums:
            continue
        nulls = sum(value is None for value in values)
        lines.append(
            f"RANGE {group}.{field} n={len(values)} null={nulls}/{len(values)} "
            f"min={min(nums):g} p50={median(nums):g} max={max(nums):g}"
        )
    by_symbol_field: dict[tuple[str, str], list[tuple[str, float]]] = defaultdict(list)
    for (tag, symbol, field), value in price_by_symbol.items():
        if tag in ("0920", "0924", "0925"):
            by_symbol_field[(symbol, field)].append((tag, value))
    for (symbol, field), tagged in sorted(by_symbol_field.items()):
        if len(tagged) > 1:
            vals = [value for _, value in tagged]
            if min(vals) > 0 and (max(vals) - min(vals)) / min(vals) > 0.5:
                flags.append(f"ANCHOR_PRICE_DELTA symbol={symbol} field={field} values={tagged}")
    return lines, flags


def trigger_rows(result: Mapping[str, Any]) -> list[str]:
    rows = []
    ordered = result.get("ordered", result)
    history = ordered.get("auction_revision_history_by_tag", {})
    for tag in TAGS:
        is_open = tag == "OPENING_0932"
        evidence = (
            ordered.get("opening_evidence", {}).get(tag, {})
            if is_open
            else ordered.get("anchor_evidence", {}).get(tag, {})
        )
        first = evidence.get("first_observable_ms")
        evaluation = evidence.get("evaluation_time_ms")
        revisions = [item for item in history.get(tag, ()) if isinstance(item, Mapping)] if not is_open else []
        if is_open:
            actual = evidence.get("evaluation_time_ms")
            revision_count = 1 if evidence else 0
            at_first = int(bool(evidence) and actual == first)
            late = []
        else:
            actual = revisions[0].get("evaluation_time_ms") if revisions else evaluation
            revision_count = len(revisions)
            at_first = sum(item.get("evaluation_time_ms") == first for item in revisions)
            late = [item.get("evaluation_time_ms") for item in revisions if item.get("evaluation_time_ms") != first]
        rows.append(
            f"TRIGGER {tag} first_observable={first} evaluation={actual} revisions={revision_count} "
            f"first_once={at_first == 1} first_count={at_first} late_revision_count={len(late)} late_times={late}"
        )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Report fact ranges from saved short-window results.")
    parser.add_argument("--result", action="append", required=True, metavar="PATH")
    parser.add_argument("--label", action="append", default=[], metavar="NAME")
    args = parser.parse_args()
    for index, path in enumerate(args.result):
        name = args.label[index] if index < len(args.label) else path.rsplit("/", 1)[-1]
        with open(path, encoding="utf-8") as source:
            result = json.load(source)
        ranges, flags = ranges_and_flags(result)
        triggers = trigger_rows(result)
        ordered = result.get("ordered", result)
        print(f"WINDOW {name} frames={ordered.get('input_frames_processed')} updates={ordered.get('input_updates_processed')}")
        print("A_NUMERIC_RANGES")
        for row in ranges:
            print(row)
        print("B_FLAGS " + ("; ".join(flags) if flags else "NONE"))
        print("C_TRIGGER_CHECK")
        for row in triggers:
            print(row)


if __name__ == "__main__":
    main()
