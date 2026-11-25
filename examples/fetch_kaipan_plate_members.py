"""Fetch point-in-time Kaipan plate membership into an offline snapshot."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _write_json(path: Path, value: Any, *, replace: bool = False) -> str:
    payload = _json_bytes(value)
    if replace:
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_bytes(payload)
        temporary.replace(path)
    else:
        with path.open("xb") as handle:
            handle.write(payload)
    return hashlib.sha256(payload).hexdigest()


def _clean_response(value: Any, secrets: set[str]) -> tuple[Any, int]:
    if isinstance(value, str):
        return ("<redacted>", 1) if value in secrets else (value, 0)
    if isinstance(value, Mapping):
        cleaned: dict[Any, Any] = {}
        count = 0
        for key, child in value.items():
            cleaned_child, child_count = _clean_response(child, secrets)
            cleaned[key] = cleaned_child
            count += child_count
        return cleaned, count
    if isinstance(value, (list, tuple)):
        cleaned = []
        count = 0
        for child in value:
            cleaned_child, child_count = _clean_response(child, secrets)
            cleaned.append(cleaned_child)
            count += child_count
        return cleaned, count
    return value, 0


def _plate_rows(response: Any) -> list[Any]:
    if not isinstance(response, Mapping) or not isinstance(response.get("list"), (list, tuple)):
        raise ValueError("UnexpectedShape")
    rows = list(response["list"])
    if any(not isinstance(row, (list, tuple)) or len(row) < 2 for row in rows):
        raise ValueError("UnexpectedShape")
    return rows


def _stock_code(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    match = re.fullmatch(r"\d{1,6}", str(value).strip())
    return match.group(0).zfill(6) if match else None


def _response_day(response: Mapping[str, Any]) -> Any:
    day = response.get("Day")
    return day[0] if isinstance(day, (list, tuple)) and day else day


def _plate_codes_file(path: str | Path) -> list[str]:
    codes = []
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        code = line.strip()
        if code and not code.startswith("#"):
            codes.append(code)
    return _dedupe_codes(codes)


def _plate_codes_csv_dir(path: str | Path) -> list[str]:
    base = Path(path)
    codes: list[str] = []
    for filename, field in (("个股板块.csv", "plateid"), ("板块.csv", "id")):
        with (base / filename).open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                value = str(row.get(field, "")).strip()
                if value:
                    codes.append(value)
    return _dedupe_codes(codes)


def _dedupe_codes(codes: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for code in codes:
        if not re.fullmatch(r"\d{6}", code):
            raise ValueError(f"invalid plate code: {code!r}")
        if code not in seen:
            seen.add(code)
            result.append(code)
    return result


def fetch_all(
    fetch: Callable[[str, str], Any],
    plate_codes: Iterable[str],
    *,
    date: str,
    output_dir: str | Path,
    sleep_s: float = 0.6,
    max_requests: int = 900,
    max_minutes: float = 40,
    consecutive_failures: int = 6,
    resume: bool = False,
    sleep_fn: Callable[[float], None] = time.sleep,
    now_fn: Callable[[], str] = _utc_now,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Fetch member responses, retaining raw payloads and a normalized relation."""
    if not date:
        raise ValueError("date is required")
    if sleep_s < 0 or max_requests < 0 or max_minutes <= 0 or consecutive_failures <= 0:
        raise ValueError("limits must be non-negative and failure threshold positive")
    codes = _dedupe_codes(str(code).strip() for code in plate_codes)
    target = Path(output_dir)
    if target.exists() and (not target.is_dir() or (any(target.iterdir()) and not resume)):
        raise FileExistsError("output directory must not exist or be empty unless --resume is set")
    target.mkdir(parents=True, exist_ok=True)
    members_dir = target / "members"
    members_dir.mkdir(exist_ok=True)

    started_at = now_fn()
    started_clock = monotonic_fn()
    request_count = 0
    consecutive_bad = 0
    failures: list[dict[str, str]] = []
    checksums: dict[str, str] = {}
    redacted_count = 0
    index: dict[str, dict[str, Any]] = {}
    relations: set[tuple[str, str]] = set()
    count_mismatch: list[str] = []
    secrets_fn = getattr(fetch, "redaction_values", None)
    secrets = set(secrets_fn()) if callable(secrets_fn) else set()
    stopped = False

    for code in codes:
        relative = f"members/{code}.json"
        member_path = target / relative
        if resume and member_path.is_file():
            try:
                saved = json.loads(member_path.read_text(encoding="utf-8"))
                rows = _plate_rows(saved)
                count_value = saved.get("Count")
                count = int(count_value) if count_value is not None else None
                day = _response_day(saved)
                got = len(rows)
                status = "EMPTY" if got == 0 else "OK"
                checksums[relative] = hashlib.sha256(member_path.read_bytes()).hexdigest()
                for row in rows:
                    stock = _stock_code(row[0])
                    if stock is not None:
                        relations.add((code, stock))
                index[code] = {"count": count, "got": got, "day": day, "status": status}
                if count is not None and count != got:
                    count_mismatch.append(code)
                continue
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
                failures.append({"plate_code": code, "error_type": type(exc).__name__})
                consecutive_bad += 1
                index[code] = {"count": None, "got": 0, "day": None, "status": "ERROR"}
                if consecutive_bad >= consecutive_failures:
                    stopped = True
                    break
                continue

        if request_count >= max_requests or (monotonic_fn() - started_clock) >= max_minutes * 60:
            stopped = True
            break
        request_count += 1
        try:
            raw_response = fetch(code, date)
            if not isinstance(raw_response, Mapping):
                raise ValueError("UnexpectedShape")
            response, redacted = _clean_response(raw_response, secrets)
            redacted_count += redacted
            rows = _plate_rows(response)
            count_value = response.get("Count")
            count = int(count_value) if count_value is not None else None
            day = _response_day(response)
            checksums[relative] = _write_json(member_path, response)
            got = len(rows)
            status = "EMPTY" if got == 0 else "OK"
            index[code] = {"count": count, "got": got, "day": day, "status": status}
            for row in rows:
                stock = _stock_code(row[0])
                if stock is not None:
                    relations.add((code, stock))
            if count is not None and count != got:
                count_mismatch.append(code)
            consecutive_bad = 0
        except Exception as exc:
            error_type = "UnexpectedShape" if isinstance(exc, ValueError) and str(exc) == "UnexpectedShape" else type(exc).__name__
            failures.append({"plate_code": code, "error_type": error_type})
            index[code] = {"count": None, "got": 0, "day": None, "status": "ERROR"}
            consecutive_bad += 1
        finally:
            sleep_fn(sleep_s)
        if consecutive_bad >= consecutive_failures:
            stopped = True
            break

    with (target / "relation.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(["plateid", "stockid"])
        writer.writerows(sorted(relations))
    checksums["relation.csv"] = hashlib.sha256((target / "relation.csv").read_bytes()).hexdigest()
    _write_json(target / "plates_index.json", index, replace=(target / "plates_index.json").exists())
    checksums["plates_index.json"] = hashlib.sha256((target / "plates_index.json").read_bytes()).hexdigest()
    ended_at = now_fn()
    complete = (not stopped and not failures and len(index) == len(codes)
                and all(item["status"] in {"OK", "EMPTY"} for item in index.values()))
    manifest = {
        "parameters": {
            "date": date, "plate_codes": codes, "sleep_s": sleep_s,
            "max_requests": max_requests, "max_minutes": max_minutes,
            "consecutive_failures": consecutive_failures, "resume": resume,
        },
        "request_count": request_count,
        "failures": failures,
        "complete": complete,
        "sha256": checksums,
        "redacted_response_count": redacted_count,
        "count_mismatch": sorted(count_mismatch),
        "started_at": started_at,
        "ended_at": ended_at,
    }
    _write_json(target / "manifest.json", manifest, replace=(target / "manifest.json").exists())
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-root", required=True)
    parser.add_argument("--date", default="")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--plate-codes-file")
    source.add_argument("--csv-dir")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--sleep-s", type=float, default=0.6)
    parser.add_argument("--max-requests", type=int, default=900)
    parser.add_argument("--max-minutes", type=float, default=40)
    parser.add_argument("--consecutive-failures", type=int, default=6)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    if not args.date:
        parser.error("--date is required")
    codes = (_plate_codes_file(args.plate_codes_file) if args.plate_codes_file
             else _plate_codes_csv_dir(args.csv_dir))
    from examples.legacy_adapters import kaipan_plate_members_callable

    fetch = kaipan_plate_members_callable(args.legacy_root)
    manifest = fetch_all(fetch, codes, date=args.date, output_dir=args.output_dir,
                         sleep_s=args.sleep_s, max_requests=args.max_requests,
                         max_minutes=args.max_minutes,
                         consecutive_failures=args.consecutive_failures, resume=args.resume)
    print(json.dumps({"request_count": manifest["request_count"],
                      "complete": manifest["complete"],
                      "failure_count": len(manifest["failures"])}, ensure_ascii=False))
    return 0 if manifest["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
