"""Shared helpers for the C567 TD sink tests: real captured daily_kline rows committed through the real catalog."""
from __future__ import annotations

import gzip
import itertools
import json
from datetime import datetime, timedelta
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "real" / "core_artifacts" / "c522" / "daily_kline" / "2026-10-23"
NROWS = 300
_TICK = itertools.count()


def load_real(variant: str = "formal"):
    manifest = json.loads((FIXTURES / variant / "manifest.json").read_text(encoding="utf-8"))
    with gzip.open(FIXTURES / variant / "rows.jsonl.gz", "rt", encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle if line.strip()]
    return manifest, rows


def seed_universe(catalog, day: str, symbols) -> None:
    """Real captured 2026-10-23 symbol universe, committed under ``day`` (coverage denominator for the writer)."""
    from conftest import read_real_artifact
    manifest, rows = read_real_artifact("symbol_universe", "2026-10-23", "actual")
    rows = [row for row in rows if str(row.get("symbol", "")).zfill(6) in set(symbols)]
    catalog.commit("symbol_universe", day, rows, manifest, source=manifest.get("source", "baostock_query_all_stock"))


def commit_day(root, day: str, provider: str, *, rows=None, symbols=None, with_universe=True):
    """Commit real captured rows as ``day`` from ``provider`` (rank/status come out of the real writer)."""
    from engine_core.data_pipeline.storage import ArtifactCatalog

    catalog = ArtifactCatalog(root)
    manifest, all_rows = load_real("formal")
    chosen = list(rows) if rows is not None else [r for r in all_rows if symbols is None or r["symbol"] in symbols][:NROWS]
    wanted = {r["symbol"] for r in all_rows[:NROWS]} | {r["symbol"] for r in chosen}
    if with_universe and catalog.locate("symbol_universe", day) is None:
        seed_universe(catalog, day, wanted)
    day_rows = [{**r, "date": day} for r in chosen]
    meta = {"schema_version": 1, "dataset": "daily_kline", "trade_date": day, "provider": provider,
            "source": provider, "available_at": day + "T18:00:00+08:00"}
    if provider == "baostock":
        meta["field_units"] = {"volume": "shares"}
    # commit instant on the trade day itself, strictly increasing: a later commit is a later version of that day
    moment = datetime.fromisoformat(day + "T18:00:00+08:00") + timedelta(seconds=next(_TICK))
    return catalog.commit("daily_kline", day, day_rows, meta, source=provider, clock=lambda: moment)
