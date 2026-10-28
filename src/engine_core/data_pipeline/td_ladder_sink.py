"""Core-owned TD sink for ``ladder_daily`` (C577), built on the C567 sink primitives (same database, same TaosStore
connection handling, same version rank and day timestamp rules). Cold data: one row per limit-up symbol per day.

Tables (database = registry ``daily_kline.td_sink.database``, ``CORE_TD_DATABASE`` overrides for acceptance runs):
``ladder_daily_v1`` (super table, child ``l_<symbol>``) and ``ladder_daily_versions`` (one row per catalog version).
Column mapping: docs/LADDER_DAILY.md section 7.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .derived_daily import LEGACY_LADDER_SOURCE, normalize_status, select_best
from .td_sink import (SHANGHAI, TaosStore, TdUnavailable, TdVerifyError, _SYMBOL, _check_date, _literal,
                      trade_day_ts_us, validate_database_name, version_rank, _parse_instant)

SUPER_TABLE = "ladder_daily_v1"
META_TABLE = "ladder_daily_versions"
SCHEMA_VERSION = 1
INSERT_CHUNK = 300
# (name, TD type, catalog row key)
COLUMNS = (("limit_up", "BOOL", "limit_up"), ("streak_n", "INT", "streak_n"),
           ("streak_start_date", "VARCHAR(10)", "streak_start_date"),
           ("day_source_chain", "VARCHAR(600)", "day_source_chain"), ("quality", "VARCHAR(64)", "quality"))
DATA_COLUMNS = tuple(c[0] for c in COLUMNS)
META_COLUMNS = ("ts", "trade_date", "version", "business_status", "rank_level", "source", "provider", "degraded",
                "quality", "row_count", "sha256", "available_at", "synced_at", "schema_version")


def ddl_statements(database: str) -> list[str]:
    db = validate_database_name(database)
    cols = ", ".join(f"{n} {t}" for n, t, _k in COLUMNS)
    return [
        f"CREATE DATABASE IF NOT EXISTS {db} PRECISION 'us' KEEP 3650d DURATION 90d VGROUPS 2",
        f"CREATE STABLE IF NOT EXISTS {db}.{SUPER_TABLE} (ts TIMESTAMP, {cols}, version VARCHAR(24)) "
        "TAGS (symbol VARCHAR(8))",
        f"CREATE TABLE IF NOT EXISTS {db}.{META_TABLE} (ts TIMESTAMP, trade_date VARCHAR(10), version VARCHAR(24), "
        "business_status VARCHAR(16), rank_level INT, source VARCHAR(96), provider VARCHAR(32), degraded BOOL, "
        "quality VARCHAR(4000), row_count INT, sha256 VARCHAR(64), available_at VARCHAR(40), synced_at VARCHAR(40), "
        "schema_version INT)",
    ]


def map_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = str(row.get("symbol") or "").zfill(6)
        if not _SYMBOL.match(symbol):
            continue
        out[symbol] = {name: row.get(key) for name, _t, key in COLUMNS}
    return out


class TaosLadderStore(TaosStore):
    """Real TDengine store for the ladder tables (reuses the C567 connection / statement runner)."""

    def schema_exists(self) -> bool:
        cursor = self._cursor()
        try:
            cursor.execute("SHOW DATABASES")
            names = {row[0] for row in cursor.fetchall()}
        except Exception as exc:
            self.close()
            raise TdUnavailable("td_statement_failed:" + str(exc)[:200]) from exc
        if self.database not in names:
            return False
        return bool(self._run(f"DESCRIBE {self.database}.{SUPER_TABLE}", missing_ok=True)) and bool(
            self._run(f"DESCRIBE {self.database}.{META_TABLE}", missing_ok=True))

    def ensure_schema(self) -> None:
        for statement in ddl_statements(self.database):
            self._run(statement)

    def read_versions(self, trade_date: str) -> list[dict[str, Any]]:
        _check_date(trade_date)
        rows = self._run(f"SELECT CAST(ts AS BIGINT), {', '.join(META_COLUMNS[1:])} FROM {self.database}.{META_TABLE} "
                         f"WHERE trade_date = '{trade_date}' ORDER BY ts", missing_ok=True)
        out = []
        for row in rows:
            item = dict(zip(META_COLUMNS, row))
            try:
                item["quality"] = json.loads(item["quality"]) if item.get("quality") else {}
            except ValueError:
                item["quality"] = {}
            out.append(item)
        return out

    def write_version(self, meta: Mapping[str, Any]) -> None:
        values = dict(meta)
        values["quality"] = json.dumps(values.get("quality") or {}, ensure_ascii=False, sort_keys=True,
                                       separators=(",", ":"))
        if len(values["quality"].encode("utf-8")) > 3800:
            values["quality"] = json.dumps({"truncated": True})
        self._run(f"INSERT INTO {self.database}.{META_TABLE} ({', '.join(META_COLUMNS)}) VALUES ("
                  + ", ".join(_literal(values.get(c)) for c in META_COLUMNS) + ")")

    def replace_day(self, trade_date: str, version: str, mapped: Mapping[str, Mapping[str, Any]]) -> None:
        _check_date(trade_date)
        ts = trade_day_ts_us(trade_date)
        self._run(f"DELETE FROM {self.database}.{SUPER_TABLE} WHERE ts = {ts}", missing_ok=True)
        items = sorted(mapped.items())
        for start in range(0, len(items), INSERT_CHUNK):
            parts = []
            for symbol, values in items[start:start + INSERT_CHUNK]:
                literal = ", ".join(_literal(values.get(n)) for n in DATA_COLUMNS)
                parts.append(f"{self.database}.l_{symbol} USING {self.database}.{SUPER_TABLE} TAGS('{symbol}') "
                             f"VALUES ({ts}, {literal}, {_literal(version)})")
            self._run("INSERT INTO " + " ".join(parts))

    def day_aggregates(self, trade_date: str) -> dict[str, Any]:
        _check_date(trade_date)
        rows = self._run(f"SELECT COUNT(*), SUM(streak_n), MAX(streak_n), COUNT(streak_start_date) FROM "
                         f"{self.database}.{SUPER_TABLE} WHERE ts = {trade_day_ts_us(trade_date)}", missing_ok=True)
        if not rows or not rows[0][0]:
            return {"count": 0, "sum_streak": 0, "max_streak": 0, "n_start": 0}
        r = rows[0]
        return {"count": int(r[0]), "sum_streak": int(r[1] or 0), "max_streak": int(r[2] or 0), "n_start": int(r[3] or 0)}

    def read_day_rows(self, trade_date: str, symbols: Sequence[str]) -> dict[str, dict[str, Any]]:
        _check_date(trade_date)
        quoted = ",".join("'" + s + "'" for s in symbols if _SYMBOL.match(s))
        if not quoted:
            return {}
        rows = self._run(f"SELECT symbol, {', '.join(DATA_COLUMNS)}, version FROM {self.database}.{SUPER_TABLE} "
                         f"WHERE ts = {trade_day_ts_us(trade_date)} AND symbol IN ({quoted})", missing_ok=True)
        return {r[0]: {**dict(zip(DATA_COLUMNS, r[1:1 + len(DATA_COLUMNS)])), "version": r[-1]} for r in rows}


class MemoryLadderStore:
    """In-memory twin used by unit tests (same semantics, can be told to fail)."""

    def __init__(self) -> None:
        self.created, self.versions, self.days, self.fail = False, {}, {}, None

    def _guard(self):
        if self.fail == "unavailable":
            raise TdUnavailable("fake_td_down")

    def schema_exists(self):
        self._guard()
        return self.created

    def ensure_schema(self):
        self._guard()
        self.created = True

    def read_versions(self, day):
        self._guard()
        return [dict(v) for v in self.versions.get(day, [])]

    def write_version(self, meta):
        self._guard()
        bucket = self.versions.setdefault(str(meta["trade_date"]), [])
        bucket[:] = [v for v in bucket if v["ts"] != meta["ts"]]
        bucket.append(dict(meta))

    def replace_day(self, day, version, mapped):
        self._guard()
        self.days[day] = {s: {**dict(v), "version": version} for s, v in mapped.items()}

    def day_aggregates(self, day):
        self._guard()
        rows = list(self.days.get(day, {}).values())
        return {"count": len(rows), "sum_streak": sum(int(r["streak_n"] or 0) for r in rows),
                "max_streak": max((int(r["streak_n"] or 0) for r in rows), default=0),
                "n_start": sum(r["streak_start_date"] is not None for r in rows)}

    def read_day_rows(self, day, symbols):
        self._guard()
        d = self.days.get(day, {})
        return {s: dict(d[s]) for s in symbols if s in d}

    def close(self):
        pass


@dataclass
class LadderOutcome:
    trade_date: str
    status: str   # synced | unchanged | skipped_lower | no_catalog_version | legacy_only
    version: str | None = None
    row_count: int | None = None
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"trade_date": self.trade_date, "status": self.status, "version": self.version,
                "row_count": self.row_count, **self.detail}


class LadderTdSink:
    def __init__(self, store: Any, root: str | Path, *, clock: Callable[[], datetime] | None = None):
        self.store, self.root = store, Path(root).expanduser()
        self.clock = clock or (lambda: datetime.now(SHANGHAI))

    def _best(self, catalog: Mapping[str, Any], day: str):
        entries = catalog.get("datasets", {}).get("ladder_daily", {}).get("dates", {}).get(day) or []
        entries = [e for e in entries if isinstance(e, Mapping)]
        if entries and all(e.get("source") == LEGACY_LADDER_SOURCE for e in entries):
            return "legacy", None
        best = select_best([e for e in entries if e.get("source") != LEGACY_LADDER_SOURCE])
        if best is None:
            return "none", None
        manifest = json.loads(Path(str(best["manifest"])).read_text(encoding="utf-8"))
        return "ok", {**manifest, **best}

    def pending_dates(self, lookback: int) -> list[str]:
        from .storage import ArtifactCatalog
        cat = ArtifactCatalog(self.root).read()
        days = sorted(cat.get("datasets", {}).get("ladder_daily", {}).get("dates", {}))
        return [d for d in days if self._best(cat, d)[0] == "ok"][-max(1, lookback):]

    def write_partition(self, day: str, rows: Sequence[Mapping[str, Any]], manifest: Mapping[str, Any]) -> LadderOutcome:
        """Persist the partition produced by this task without rereading catalog as a source."""
        _check_date(day)
        best = dict(manifest)
        if str(best.get("trade_date") or day) != day:
            raise TdVerifyError("td_ladder_partition_date_mismatch:" + day)
        row_count = int(best.get("row_count") if best.get("row_count") is not None else len(rows))
        if row_count != len(rows):
            raise TdVerifyError(f"td_ladder_partition_row_count_mismatch:{day}:{row_count}:{len(rows)}")
        mapped = map_rows(rows)
        if len(mapped) != len(rows):
            raise TdVerifyError(f"td_ladder_partition_invalid_symbols:{day}:{len(rows)}:{len(mapped)}")
        if not self.store.schema_exists():
            self.store.ensure_schema()
        existing = self.store.read_versions(day)
        key = version_rank(best.get("business_status"), False)
        for held in existing:
            if version_rank(held.get("business_status"), False) > key:
                return LadderOutcome(day, "skipped_lower", str(held["version"]), int(held.get("row_count") or 0),
                                     {"catalog_version": best["version"]})
        same = [h for h in existing if h["version"] == best["version"]]
        if same:
            agg = self.store.day_aggregates(day)
            same_meta = same[-1]
            if (agg.get("count") == len(mapped)
                    and int(same_meta.get("row_count") or -1) == len(mapped)
                    and str(same_meta.get("sha256") or "") == str(best.get("sha256") or "")):
                return LadderOutcome(day, "unchanged", str(best["version"]), len(mapped))
        self.store.replace_day(day, str(best["version"]), mapped)
        self._verify(day, best, rows, mapped)
        moment = _parse_instant(best.get("available_at") or best.get("committed_at"))
        ts = int(moment.timestamp() * 1_000_000) if moment else trade_day_ts_us(day)
        taken = {int(h["ts"]) for h in existing if h["version"] != best["version"]}
        ts = next((int(h["ts"]) for h in same), ts)
        while ts in taken:
            ts += 1
        q = best.get("quality") if isinstance(best.get("quality"), Mapping) else {}
        keep = {k: q.get(k) for k in ("partial", "streak_lower_bound_count", "chain_truncated_days", "window_capped",
                                      "days_visited", "max_streak") if k in q}
        self.store.write_version({"ts": ts, "trade_date": day, "version": str(best["version"]),
                                  "business_status": normalize_status(best.get("business_status")),
                                  "rank_level": version_rank(best.get("business_status"), False)[0],
                                  "source": str(best.get("source") or "")[:96],
                                  "provider": str(best.get("provider") or "")[:32],
                                  "degraded": bool(best.get("degraded")), "quality": keep,
                                  "row_count": len(mapped), "sha256": str(best.get("sha256") or ""),
                                  "available_at": str(best.get("available_at") or ""),
                                  "synced_at": self.clock().isoformat(), "schema_version": SCHEMA_VERSION})
        readback = next((item for item in self.store.read_versions(day)
                         if str(item.get("version")) == str(best["version"])), None)
        final_count = self.store.day_aggregates(day).get("count")
        if (readback is None or str(readback.get("sha256") or "") != str(best.get("sha256") or "")
                or int(readback.get("row_count") or -1) != len(mapped) or int(final_count or 0) != len(mapped)):
            raise TdVerifyError("td_ladder_version_readback_mismatch:" + day)
        return LadderOutcome(day, "synced", str(best["version"]), len(mapped),
                             {"previous_versions": [h["version"] for h in existing]})

    def sync_date(self, day: str) -> LadderOutcome:
        """Compatibility helper for explicit catalog reconciliation; producers use write_partition."""
        from .storage import ArtifactCatalog
        catalog = ArtifactCatalog(self.root)
        state, best = self._best(catalog.read(), day)
        if state == "legacy":
            return LadderOutcome(day, "legacy_only", detail={"reason": "only_pre_C577_partition"})
        if best is None:
            return LadderOutcome(day, "no_catalog_version")
        rows = catalog.read_rows(Path(str(best["manifest"])))
        return self.write_partition(day, rows, best)

    def _verify(self, day: str, best: Mapping[str, Any], rows, mapped) -> None:
        agg = self.store.day_aggregates(day)
        want = {"count": len(mapped), "sum_streak": sum(int(v["streak_n"] or 0) for v in mapped.values()),
                "max_streak": max((int(v["streak_n"] or 0) for v in mapped.values()), default=0),
                "n_start": sum(v["streak_start_date"] is not None for v in mapped.values())}
        problems = [f"{k}: catalog={want[k]} td={agg[k]}" for k in want if want[k] != agg[k]]
        sample = sorted(mapped)[:: max(1, len(mapped) // 100 or 1)][:120]
        got = self.store.read_day_rows(day, sample)
        for s in sample:
            row = got.get(s)
            if row is None or row.get("version") != best["version"] or any(
                    row.get(c) != mapped[s][c] for c in DATA_COLUMNS):
                problems.append(f"sample {s}: catalog={mapped[s]} td={row}")
                break
        if problems:
            raise TdVerifyError("td_readback_mismatch:" + day + ":" + "; ".join(problems[:4]))

    def sync_dates(self, days: Sequence[str]) -> list[LadderOutcome]:
        return [self.sync_date(d) for d in sorted(set(days))]
