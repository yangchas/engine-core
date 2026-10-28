#!/usr/bin/env python3
"""Record a bounded, anonymous WenCai pagination run as raw page JSON fixtures.

The query limits the universe to codes below 600000 so one complete run stays within
the 40-page daily cap. Pages are fetched in order; only pages 1, 2, 3, and the final
page are retained. A started run is never retried automatically.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import math
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
RAW_DIR = ROOT / "tests" / "fixtures" / "wencai_raw"
PROVENANCE = RAW_DIR / "PROVENANCE.json"
TZ = ZoneInfo("Asia/Shanghai")
PAGE_SIZE = 100
MAX_DAILY_REQUESTS = 40
FIXED_SETUP_REQUESTS = 2  # one screener warmup plus the robot query for its pagination URL
MAX_PAGE_REQUESTS = MAX_DAILY_REQUESTS - FIXED_SETUP_REQUESTS
MIN_PAGE_INTERVAL_SECONDS = 10.0
SAFE_CAPTURE_ERRORS = {
    "capture_already_started; refusing a second upstream run",
    "upstream_pagination_url_missing", "raw_page_response_not_unique",
    "raw_page_schema_or_secret_guard_failed", "raw_page_empty_or_unexpected",
    "upstream_page_exceeds_100_rows", "upstream_row_count_missing",
    "upstream_row_count_empty", "query_too_small_for_page_boundary_fixture",
    "query_exceeds_40_request_daily_cap", "upstream_row_count_changed_during_pagination",
    "pagination_did_not_reach_reported_last_page", "captured_page_set_incomplete",
}


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=1).encode("utf-8") + b"\n"


def _write_index(index: dict) -> None:
    tmp = PROVENANCE.with_name("PROVENANCE.json.tmp")
    tmp.write_bytes(_json_bytes(index))
    tmp.replace(PROVENANCE)


def _contains_secret_key(value: object) -> bool:
    forbidden = {"cookie", "set-cookie", "authorization", "access_token", "token", "hexin-v", "secret"}
    if isinstance(value, dict):
        return any(str(key).lower() in forbidden or _contains_secret_key(child)
                   for key, child in value.items())
    if isinstance(value, list):
        return any(_contains_secret_key(item) for item in value)
    return False


@contextmanager
def _wencai_run(query: str):
    import requests
    from pywencai import wencai

    captured_bodies: list[bytes] = []
    original_post = requests.Session.post
    original_get = requests.Session.get
    last_request_started: float | None = None

    def pace_all_requests() -> None:
        nonlocal last_request_started
        if last_request_started is not None:
            remaining = MIN_PAGE_INTERVAL_SECONDS - (time.monotonic() - last_request_started)
            if remaining > 0:
                time.sleep(remaining)
        last_request_started = time.monotonic()

    def checked_post(session, url, *args, **kwargs):
        pace_all_requests()
        response = original_post(session, url, *args, **kwargs)
        if url == wencai.LANDING_DATA_URL:
            if response.status_code in (401, 403):
                raise wencai.WencaiAuthError("WenCai landing request rejected")
            captured_bodies.append(response.content)
        elif response.status_code in (401, 403):
            raise wencai.WencaiAuthError("WenCai request rejected")
        return response

    def checked_get(session, url, *args, **kwargs):
        pace_all_requests()
        response = original_get(session, url, *args, **kwargs)
        if response.status_code in (401, 403):
            raise wencai.WencaiAuthError("WenCai session request rejected")
        return response

    requests.Session.post = checked_post
    requests.Session.get = checked_get
    session = None
    try:
        session = wencai._new_session(None)
        request_options, timeout = wencai._request_options(None)
        user_agent = wencai.get_user_agent(None)
        robot_payload, token, server_time, rsh = wencai._robot_request_in_session(
            session, question=query, query_type="stock", request_options=request_options,
            timeout=timeout, resolved_ua=user_agent,
            kwargs={"urp_sort_index": "股票代码", "urp_sort_way": "asc"}, log=False,
        )
        footer_url = wencai._extract_footer_url(robot_payload)
        if not footer_url:
            raise RuntimeError("upstream_pagination_url_missing")
        _, _, robot_total = wencai._extract_table(robot_payload)

        def fetch_page(page: int) -> tuple[bytes, list[dict], int | None]:
            captured_bodies.clear()
            payload = wencai._landing_request_in_session(
                session, question=query, query_type="stock", footer_url=footer_url,
                page=page, perpage=PAGE_SIZE, request_options=request_options,
                timeout=timeout, resolved_ua=user_agent, token=token,
                server_time=server_time,
                kwargs={"urp_sort_index": "股票代码", "urp_sort_way": "asc"},
            )
            if len(captured_bodies) != 1:
                raise RuntimeError("raw_page_response_not_unique")
            body = captured_bodies[0]
            parsed = json.loads(body.decode("utf-8"))
            if not isinstance(parsed, dict) or _contains_secret_key(parsed):
                raise RuntimeError("raw_page_schema_or_secret_guard_failed")
            rows, has_table, page_total = wencai._extract_table(parsed)
            if not has_table or not rows:
                raise RuntimeError("raw_page_empty_or_unexpected")
            if len(rows) > PAGE_SIZE:
                raise RuntimeError("upstream_page_exceeds_100_rows")
            total = robot_total if robot_total is not None else page_total
            return body, rows, total

        yield fetch_page
    finally:
        if session is not None:
            session.close()
        requests.Session.post = original_post
        requests.Session.get = original_get


def day_query(day: str) -> str:
    from engine_core.data_pipeline.providers.wencai_daily_adapter import build_daily_kline_query

    # This keeps the adapter's dated field selection and units while limiting the
    # one-time recording below the documented 40 page-request/day ceiling.
    return build_daily_kline_query(day) + ";股票代码小于600000"


def _capture(day: str) -> dict:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    index = json.loads(PROVENANCE.read_text(encoding="utf-8")) if PROVENANCE.exists() else {"captures": {}}
    index.setdefault("captures", {})
    existing = index["captures"].get(day)
    if existing:
        if existing.get("status") == "complete":
            return existing
        raise RuntimeError("capture_already_started; refusing a second upstream run")

    query = day_query(day)
    started_at = datetime.now(TZ).isoformat(timespec="seconds")
    capture = {
        "status": "started", "source": "iwencai landing getDataList raw JSON",
        "trade_date": day, "captured_at": started_at, "query": query,
        "page_size": PAGE_SIZE, "authentication": "anonymous",
        "max_daily_requests": MAX_DAILY_REQUESTS,
        "fixed_setup_requests": FIXED_SETUP_REQUESTS,
    }
    index["captures"][day] = capture
    _write_index(index)

    started = time.monotonic()
    last_started: float | None = None
    files: dict[str, dict] = {}
    total_rows: int | None = None
    total_pages: int | None = None
    with _wencai_run(query) as fetch_page:
        for page in range(1, MAX_PAGE_REQUESTS + 1):
            if last_started is not None:
                remaining = MIN_PAGE_INTERVAL_SECONDS - (time.monotonic() - last_started)
                if remaining > 0:
                    time.sleep(remaining)
            last_started = time.monotonic()
            body, rows, reported_value = fetch_page(page)
            try:
                reported = int(reported_value)
            except (TypeError, ValueError):
                raise RuntimeError("upstream_row_count_missing") from None
            if reported <= 0:
                raise RuntimeError("upstream_row_count_empty")
            if total_rows is None:
                total_rows = reported
                total_pages = max(1, math.ceil(total_rows / PAGE_SIZE))
                if total_pages <= 3:
                    raise RuntimeError("query_too_small_for_page_boundary_fixture")
                # Count one warmup, one semantic parse, and every page against the daily cap.
                if total_pages + FIXED_SETUP_REQUESTS > MAX_DAILY_REQUESTS:
                    raise RuntimeError("query_exceeds_40_request_daily_cap")
            elif reported != total_rows:
                raise RuntimeError("upstream_row_count_changed_during_pagination")

            if page in {1, 2, 3, total_pages}:
                filename = f"wencai_{day}_page_{page:03d}.json"
                path = RAW_DIR / filename
                path.write_bytes(body)
                files[str(page)] = {
                    "file": filename, "rows": len(rows), "bytes": len(body),
                    "sha256": hashlib.sha256(body).hexdigest(),
                }
            if page == total_pages:
                break
        else:
            raise RuntimeError("pagination_did_not_reach_reported_last_page")

    if total_pages is None or set(files) != {str(page) for page in sorted({1, 2, 3, total_pages})}:
        raise RuntimeError("captured_page_set_incomplete")
    capture.update({
        "status": "complete", "finished_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "total_rows": total_rows, "total_pages": total_pages,
        "pages_scanned": total_pages, "captured_pages": sorted(int(page) for page in files),
        "files": files, "elapsed_seconds": round(time.monotonic() - started, 3),
    })
    _write_index(index)
    return capture


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=datetime.now(TZ).date().isoformat())
    args = parser.parse_args(argv)
    try:
        capture = _capture(args.date)
    except Exception as exc:  # keep response bodies, URL, and authentication material out of errors
        detail = str(exc) if str(exc) in SAFE_CAPTURE_ERRORS else "upstream_error"
        print(f"capture failed: {type(exc).__name__}: {detail}", file=sys.stderr)
        return 1
    print(json.dumps({"status": capture["status"], "trade_date": capture["trade_date"],
                      "page_size": capture["page_size"], "total_rows": capture.get("total_rows"),
                      "total_pages": capture.get("total_pages"),
                      "upstream_requests": capture.get("total_pages", 0) + FIXED_SETUP_REQUESTS,
                      "captured_pages": capture.get("captured_pages")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
