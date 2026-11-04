#!/usr/bin/env python3
"""Record what the REAL code asks TD and what TD really answers, for replay in tests (tests/replay.py).

  record_td_scenario.py --list
  record_td_scenario.py NAME [NAME ...]      # needs TD_CONFIG; only SELECT/SHOW statements are ever sent

Each scenario runs a real function against the real database through a recording wrapper. ``inject`` entries make the
wrapper raise a REAL recorded error for matching statements (the error text comes from tests/fixtures/real/td/
errors/*.json, itself copied from a production ledger entry) so the code's recovery path produces further real
statements to record. Output: tests/fixtures/real/td/<NAME>.json.gz and its entry in tests/fixtures/real/PROVENANCE.json
(`_td_recordings`).
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
from tests.replay import FIXTURES, encode_value, normalize  # noqa: E402

PROVENANCE = ROOT / "tests" / "fixtures" / "real" / "PROVENANCE.json"
READ_ONLY = re.compile(r"^\s*(select|show|describe)\b", re.IGNORECASE)


class RecordingCursor:
    def __init__(self, inner, statements, inject):
        self.inner, self.statements, self.inject = inner, statements, inject
        self._last = None
        self.description = None

    def execute(self, sql, *args, **kwargs):
        if not READ_ONLY.match(sql):
            raise RuntimeError("recorder refuses non-read-only statement")
        for needle, error in self.inject:
            if needle(sql):
                self.statements.append({"sql": normalize(sql), "error": error})
                self._last = None
                raise RuntimeError(error["message"])
        result = self.inner.execute(sql, *args, **kwargs)
        rows = [tuple(row) for row in self.inner.fetchall()]
        columns = [d[0] for d in (self.inner.description or [])]
        self.statements.append({"sql": normalize(sql), "columns": columns,
                                "rows": [[encode_value(v) for v in row] for row in rows], "row_count": len(rows)})
        self._rows = rows
        self.description = self.inner.description
        return result

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class RecordingConnection:
    def __init__(self, inner, statements, inject):
        self.inner, self.statements, self.inject = inner, statements, inject

    def cursor(self):
        return RecordingCursor(self.inner.cursor(), self.statements, self.inject)

    def close(self):
        self.inner.close()


def real_error(name):
    return json.loads((FIXTURES / "errors" / f"{name}.json").read_text(encoding="utf-8"))


def _connect(statements, inject):
    from engine_core import plate_amount_baseline as module
    return module, RecordingConnection(module._td_connect(), statements, inject)


def scenario_plate_amount_2day(statements):
    module, connection = _connect(statements, [])
    module._td_connect = lambda: connection
    module._read_td("2026-11-02", ["2026-10-29", "2026-10-30"])
    return {"function": "plate_amount_baseline._read_td", "args": ["2026-11-02", ["2026-10-29", "2026-10-30"]]}


def scenario_plate_amount_slice_halving(statements):
    """The 2-day tick slice runs out of memory (real error), so the code halves it into two 1-day slices."""
    two_day = lambda sql: "stock_tick_v2" in sql and "2026-10-29 09:30:00" in sql and "2026-10-30 09:45:00" in sql
    module, connection = _connect(statements, [(two_day, real_error("query_memory_exhausted_0x073a"))])
    module._td_connect = lambda: connection
    module._read_td("2026-11-02", ["2026-10-29", "2026-10-30"])
    return {"function": "plate_amount_baseline._read_td", "args": ["2026-11-02", ["2026-10-29", "2026-10-30"]],
            "injected": "2-day tick slice -> real error 0x073a"}


def scenario_plate_amount_symbol_split(statements):
    """A single day still runs out of memory (real error) on the unfiltered statement -> split by symbol range."""
    unfiltered = lambda sql: "stock_tick_v2" in sql and "symbol >=" not in sql
    module, connection = _connect(statements, [(unfiltered, real_error("query_memory_exhausted_0x073a"))])
    module._td_connect = lambda: connection
    module._read_td("2026-11-02", ["2026-10-30"])
    return {"function": "plate_amount_baseline._read_td", "args": ["2026-11-02", ["2026-10-30"]],
            "injected": "unfiltered 1-day tick slice -> real error 0x073a"}


def scenario_plate_amount_auction_halving(statements):
    """A multi-day auction slice runs out of memory (real error) -> halved."""
    multi = lambda sql: "auction_snapshot_v2" in sql and sql.count('"2026') > 1
    module, connection = _connect(statements, [(multi, real_error("query_memory_exhausted_0x073a"))])
    module._td_connect = lambda: connection
    module._read_auction_rows(connection.cursor(), ["2026-10-29", "2026-10-30"])
    return {"function": "plate_amount_baseline._read_auction_rows", "args": [["2026-10-29", "2026-10-30"]],
            "injected": "2-day auction slice -> real error 0x073a"}


def scenario_dde_suspended(statements):
    from engine_core.data_pipeline.daily_dde import TaosDdeStore
    store = TaosDdeStore("core_data")
    inner = store._cursor
    connection = RecordingConnection(type("C", (), {"cursor": lambda self: inner(), "close": lambda self: None})(),
                                     statements, [])
    store._cursor = connection.cursor
    store.read_suspended(["000831"], "2026-11-02")
    return {"function": "TaosDdeStore.read_suspended", "args": [["000831"], "2026-11-02"]}


def scenario_dde_not_flagged_1103(statements):
    """000831 has no daily_kline row for 2026-11-03: TD flags nothing as suspended, so a stale DDE stays a failure."""
    from engine_core.data_pipeline.daily_dde import TaosDdeStore
    store = TaosDdeStore("core_data")
    inner = store._cursor
    connection = RecordingConnection(type("C", (), {"cursor": lambda self: inner(), "close": lambda self: None})(),
                                     statements, [])
    store._cursor = connection.cursor
    store.read_suspended(["000831"], "2026-11-03")
    return {"function": "TaosDdeStore.read_suspended", "args": [["000831"], "2026-11-03"]}


def scenario_plate_amount_other_error(statements):
    """A tick statement fails with an error that is NOT memory exhaustion (real error text); it must surface labelled."""
    tick = lambda sql: "stock_tick_v2" in sql
    module, connection = _connect(statements, [(tick, real_error("table_does_not_exist"))])
    module._td_connect = lambda: connection
    try:
        module._read_td("2026-11-02", ["2026-10-30"])
    except RuntimeError:
        pass
    return {"function": "plate_amount_baseline._read_td", "args": ["2026-11-02", ["2026-10-30"]],
            "injected": "tick statement -> real error 'table does not exist'"}


def scenario_show_queries(statements):
    """SHOW QUERIES as it is right now, with the real command line of each client pid (for owner classification)."""
    from engine_core.guardian import td_resource_check as check
    connection = RecordingConnection(check.connect(), statements, [])
    queries = check.fetch_queries(connection.cursor())
    owners = {str(q["pid"]): check.owner_command(q["pid"]) for q in queries}
    return {"function": "td_resource_check.fetch_queries", "owners": owners}


SCENARIOS = {
    "show_queries": scenario_show_queries,
    "plate_amount_2day": scenario_plate_amount_2day,
    "plate_amount_slice_halving": scenario_plate_amount_slice_halving,
    "plate_amount_symbol_split": scenario_plate_amount_symbol_split,
    "plate_amount_auction_halving": scenario_plate_amount_auction_halving,
    "dde_suspended": scenario_dde_suspended,
    "dde_not_flagged_1103": scenario_dde_not_flagged_1103,
    "plate_amount_other_error": scenario_plate_amount_other_error,
}


def output_reference_snapshot_plate_members():
    """The real return value of ReferenceSnapshotStore.read_latest('plate_members') without its 91k payload rows
    (its integrity check reads them all; what callers use is the marker: version, row_count, sha256, trade_date)."""
    from engine_core.data_pipeline.reference_snapshot_td import ReferenceSnapshotStore
    with ReferenceSnapshotStore() as store:
        latest = store.read_latest("plate_members")
    rows = latest.pop("rows")
    latest["rows_omitted"] = len(rows)
    return {"function": "ReferenceSnapshotStore.read_latest", "args": ["plate_members"]}, latest


OUTPUTS = {"reference_snapshot_plate_members_latest": output_reference_snapshot_plate_members}


def record_output(name):
    call, value = OUTPUTS[name]()
    now = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
    directory = FIXTURES / "outputs"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    path.write_text(json.dumps({"recorded_at": now, "call": call, "value": value}, ensure_ascii=False, indent=1,
                               sort_keys=True) + "\n", encoding="utf-8")
    provenance = json.loads(PROVENANCE.read_text(encoding="utf-8"))
    provenance.setdefault("_td_recordings", {})[f"td/outputs/{name}.json"] = {
        "kind": "raw_td_function_output", "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "bytes": path.stat().st_size, "recorded_at": now, "call": call,
        "command": f"examples/record_td_scenario.py {name}", "source": "core TD (read-only)"}
    PROVENANCE.write_text(json.dumps(provenance, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{name}: {json.dumps(value, ensure_ascii=False)[:200]}")


def record(name):
    if name in OUTPUTS:
        return record_output(name)
    statements: list[dict] = []
    call = SCENARIOS[name](statements)
    now = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
    body = {"scenario": name, "recorded_at": now, "database_env": "TD_CONFIG", "call": call, "statements": statements}
    FIXTURES.mkdir(parents=True, exist_ok=True)
    path = FIXTURES / f"{name}.json.gz"
    with gzip.GzipFile(filename="", mode="wb", fileobj=open(path, "wb"), mtime=0) as handle:
        handle.write(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    provenance = json.loads(PROVENANCE.read_text(encoding="utf-8"))
    provenance.setdefault("_td_recordings", {})[f"td/{name}.json.gz"] = {
        "kind": "raw_td_result", "scenario": name, "sha256": digest, "bytes": path.stat().st_size,
        "recorded_at": now, "statements": len(statements), "rows": sum(s.get("row_count", 0) for s in statements),
        "errors": sum(1 for s in statements if "error" in s), "command": f"examples/record_td_scenario.py {name}",
        "call": call, "source": "core TD (read-only SELECT/SHOW)"}
    PROVENANCE.write_text(json.dumps(provenance, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{name}: {len(statements)} statements, {sum(s.get('row_count', 0) for s in statements)} rows, "
          f"{path.stat().st_size} bytes")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("names", nargs="*")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)
    if args.list or not args.names:
        print("\n".join([*SCENARIOS, *OUTPUTS]))
        return 0
    if not os.environ.get("TD_CONFIG"):
        print("TD_CONFIG is required", file=sys.stderr)
        return 2
    for name in args.names:
        record(name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
