"""C632 fill + C631 postmarket: the day becomes publishable exactly when the fill has published its marker."""
import json

from engine_core.data_pipeline.daily_kline_fill import publish_filled_days, run_daily_kline_fill
from engine_core.data_pipeline.ops import run_daily_phase
from engine_core.data_pipeline.td_sink import MemoryTdStore
from tests.test_daily_kline_fill import DAYS, SYMBOLS, TARGET, FixtureClient
from tests.test_daily_kline_gapfill import _seed_real_symbol_universe


def _calendar(root):
    path = root / "trade_calendar" / "calendar.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema": "CoreTradeCalendarMatrixV1", "lit_from": DAYS[0], "lit_to": DAYS[-1],
                                "days": [{"d": day, "t": 1} for day in DAYS]}))


def _fill(root, store, *, publish):
    kwargs = {"on_complete": lambda r: publish_filled_days(root, store, r["publish_days"],
                                                           lambda _d: set(SYMBOLS), 0.995)} if publish else {}
    return run_daily_kline_fill(TARGET, store=store, client=FixtureClient(), trading_dates=DAYS,
                                expected_symbols=lambda _d: set(SYMBOLS), depth=5, coverage_threshold=0.995, **kwargs)


def _postmarket(root):
    return run_daily_phase("postmarket", TARGET, root=root, adapters={}, trading_dates=None, write=False,
                           now=f"{TARGET}T18:00:00+08:00", lock=False, fetch_daily_kline=False)


def test_postmarket_waits_then_proceeds_after_a_killed_fill_is_rerun(tmp_path):
    _calendar(tmp_path)
    for day in DAYS:
        _seed_real_symbol_universe(tmp_path, day, sorted(SYMBOLS))
    store = MemoryTdStore()

    assert _postmarket(tmp_path)["error_code"] == "DAILY_KLINE_NOT_READY"      # nothing fetched yet
    _fill(tmp_path, store, publish=False)                                      # killed before the marker
    assert _postmarket(tmp_path)["error_code"] == "DAILY_KLINE_NOT_READY"      # rows alone are not "ready"
    rerun = _fill(tmp_path, store, publish=True)                               # the next trigger point
    assert rerun["status"] == "COMPLETE" and rerun["written_symbols"] == 0     # no refetch, only the marker
    ready = _postmarket(tmp_path)
    assert ready.get("error_code") != "DAILY_KLINE_NOT_READY"
    assert ready["jobs"][0]["job"] == "daily_kline" and ready["jobs"][0]["status"] == "SKIPPED_ALREADY_PUBLISHED"
    assert _fill(tmp_path, store, publish=True)["status"] == "NO_GAPS"         # and a finished day stays untouched


def test_a_provisional_day_with_enough_coverage_unblocks_postmarket_and_is_not_republished(tmp_path):
    # The shape of the real 2026-10-19 / 10-26 partitions: coverage met, FORMAL impossible (empty volume row).
    from engine_core.data_pipeline.published_daily_kline import find_published_baostock_manifest
    from engine_core.data_pipeline.storage import ArtifactCatalog

    class GappyVolume(FixtureClient):
        def query(self, symbol, start, end):
            return [{**row, "volume": None} if symbol == "000001" else row
                    for row in super().query(symbol, start, end)]

    _calendar(tmp_path)
    for day in DAYS:
        _seed_real_symbol_universe(tmp_path, day, sorted(SYMBOLS))
    store, catalog, published = MemoryTdStore(), ArtifactCatalog(tmp_path), []

    def run():
        return run_daily_kline_fill(
            TARGET, store=store, client=GappyVolume(), trading_dates=DAYS, expected_symbols=lambda _d: set(SYMBOLS),
            depth=5, coverage_threshold=0.995,
            needs_publish=lambda day: find_published_baostock_manifest(catalog, day, 0.995) is None,
            on_complete=lambda r: published.append(publish_filled_days(
                tmp_path, store, r["publish_days"], lambda _d: set(SYMBOLS), 0.995)))

    assert _postmarket(tmp_path)["error_code"] == "DAILY_KLINE_NOT_READY"
    assert run()["status"] == "COMPLETE"
    assert {p["business_status"] for p in published[0]} == {"PROVISIONAL"}
    assert _postmarket(tmp_path).get("error_code") != "DAILY_KLINE_NOT_READY"
    assert run()["status"] == "NO_GAPS" and len(published) == 1
