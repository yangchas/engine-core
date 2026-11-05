# DATA: real:td/plate_amount_day_1030 (a whole day), td/plate_amount_day_1029 (a day whose auction snapshots TD does not have) - one-day recordings of the statements the code issues (examples/record_td_scenario.py); Redis is the in-memory double
"""C690: a finished day's amounts are read from TD once and kept; later runs read only the days they do not have."""
import json

import pytest

from engine_core import plate_amount_baseline as module
from engine_core.runtime_state import StateStore
from tests.memory_redis import MemoryRedis
from tests.replay import ReplayTd, load_recording

WHOLE, THIN = "2026-10-30", "2026-10-29"      # the real 10-29 auction snapshots are empty: only its 09:45 amounts exist


def _key(day):
    return module.OBS_KEY_PREFIX + day


def _td(monkeypatch, *names):
    connection = ReplayTd(*names)
    monkeypatch.setattr(module, "_td_connect", lambda: connection)
    return connection


def _no_td(monkeypatch):
    def forbidden():
        raise AssertionError("TD was used for a day that is kept")
    monkeypatch.setattr(module, "_td_connect", forbidden)


def _as_set(rows):
    return {(r["trade_date"], r["slot"], r["symbol"], r["amount_yuan"]) for r in rows}


@pytest.fixture
def store():
    return StateStore(MemoryRedis())


def test_a_whole_day_is_kept_and_the_next_read_does_not_touch_td(monkeypatch, store):
    _td(monkeypatch, "plate_amount_day_1030")
    first, stats = module.read_observations([WHOLE], store=store)
    assert stats == {"from_cache": 0, "from_td": 1, "kept": 1} and first
    _no_td(monkeypatch)
    again, stats = module.read_observations([WHOLE], store=store)
    assert stats == {"from_cache": 1, "from_td": 0, "kept": 0}
    assert _as_set(again) == _as_set(first)                      # exactly what TD gave, nothing added or lost


def test_a_day_with_a_missing_slot_is_never_kept(monkeypatch, store):
    _td(monkeypatch, "plate_amount_day_1029")
    rows, stats = module.read_observations([THIN], store=store)
    assert {r["slot"] for r in rows} == {"0945"} and stats == {"from_cache": 0, "from_td": 1, "kept": 0}
    assert store.get(_key(THIN)) is None


def test_only_the_days_that_are_not_kept_are_read_from_td(monkeypatch, store):
    _td(monkeypatch, "plate_amount_day_1030")
    module.read_observations([WHOLE], store=store)
    connection = _td(monkeypatch, "plate_amount_day_1029")
    rows, stats = module.read_observations([THIN, WHOLE], store=store)
    recorded = [entry["sql"] for entry in load_recording("plate_amount_day_1029")["statements"]]
    assert connection.statements == recorded                       # the statements of the missing day only
    assert stats == {"from_cache": 1, "from_td": 1, "kept": 0}
    assert {r["trade_date"] for r in rows} == {THIN, WHOLE}


@pytest.mark.parametrize("damage", [
    lambda value: {**value, "0925": dict(list(value["0925"].items())[:10])},       # a thin slot
    lambda value: {"0925": value["0925"]},                                          # a slot missing
    lambda value: {**value, "0945": {symbol: "x" for symbol in value["0945"]}},     # amounts that are not integers
    lambda value: ["not", "a", "mapping"],
])
def test_a_kept_day_that_does_not_look_whole_is_read_again(monkeypatch, store, damage):
    _td(monkeypatch, "plate_amount_day_1030")
    module.read_observations([WHOLE], store=store)
    store.set(_key(WHOLE), damage(store.get(_key(WHOLE))), 3600)
    _td(monkeypatch, "plate_amount_day_1030")
    _, stats = module.read_observations([WHOLE], store=store)
    assert stats == {"from_cache": 0, "from_td": 1, "kept": 1}        # read again, and the whole copy replaces the damaged one


def test_an_unreachable_redis_only_costs_the_saving(monkeypatch):
    down = StateStore(MemoryRedis(down=True))
    _td(monkeypatch, "plate_amount_day_1030")
    rows, stats = module.read_observations([WHOLE], store=down)
    assert rows and stats == {"from_cache": 0, "from_td": 1, "kept": 0}


def test_without_a_store_every_day_comes_from_td(monkeypatch):
    _td(monkeypatch, "plate_amount_day_1030")
    rows, stats = module.read_observations([WHOLE], store=None)
    assert rows and stats == {"from_cache": 0, "from_td": 1, "kept": 0}


def test_the_kept_copy_is_small_and_expires(monkeypatch, store):
    _td(monkeypatch, "plate_amount_day_1030")
    module.read_observations([WHOLE], store=store)
    redis = store._client
    assert 0 < redis.ttls[_key(WHOLE)] <= module.OBS_TTL_SECONDS
    assert len(redis.values[_key(WHOLE)]) < 400_000                  # two slots x ~5200 symbols
    assert set(json.loads(redis.values[_key(WHOLE)])) == {"0925", "0945"}
