import json

import pytest

from examples.run_core_limit_up_pool_cache import _load_eod_capture, run_probe


class FakeCursor:
    def __init__(self, rows_by_table):
        self.rows_by_table = rows_by_table
        self.current = None

    def execute(self, sql):
        table = sql.split("FROM ", 1)[1].split(" ", 1)[0]
        if table not in self.rows_by_table:
            raise RuntimeError("missing table")
        self.current = self.rows_by_table[table]

    def fetchall(self):
        return self.current


class FakeTD:
    def __init__(self, rows_by_table):
        self.cursor_obj = FakeCursor(rows_by_table)

    def cursor(self):
        return self.cursor_obj

    def close(self):
        pass


class FakeRedis:
    def __init__(self, hashes=None):
        self.hashes = hashes or {}
        self.values = {}
        self.calls = []

    def type(self, key):
        return "hash" if key in self.hashes else "none"

    def hlen(self, key):
        return len(self.hashes.get(key, {}))

    def hscan_iter(self, key):
        return iter(self.hashes.get(key, {}).items())

    def get(self, key):
        return self.values.get(key)

    def ttl(self, key):
        return 100

    def hset(self, key, mapping):
        self.calls.append(("hset", key, mapping))
        self.hashes[key] = mapping

    def set(self, key, value, ex=None):
        self.calls.append(("set", key, value, ex))
        self.values[key] = value

    def expire(self, key, ttl):
        self.calls.append(("expire", key, ttl))

    def rename(self, source, target):
        self.calls.append(("rename", source, target))
        self.hashes[target] = self.hashes.pop(source)


def _daily_row(day="2026-10-12"):
    return (day + " 15:00:00", 10.0, 11.0, 9.0, 11.0, 100.0, 1234.0, 1.0, 10.0)


def _run(monkeypatch, *, td_rows, redis=None, apply=False, confirm=False, names=None):
    redis = redis or FakeRedis()
    clients = []

    def client_factory():
        clients.append(FakeRedis())
        return clients[-1]

    result = run_probe(
        trade_date="2026-10-13",
        previous_trade_date="2026-10-12",
        symbols=("600001", "600002"),
        lookback_start="2026-09-12",
        td_connect=lambda **kwargs: FakeTD(td_rows),
        redis_read_client=redis,
        redis_write_client_factory=client_factory,
        names_by_symbol=names or {},
        apply=apply,
        confirm_write_core_cache=confirm,
    )
    return result, clients, redis


def test_default_dry_run_does_not_create_write_client_and_reports_comparison():
    legacy = FakeRedis({"cache:yest_limit_pool:2026-10-12": {
        "600001": json.dumps({"symbol": "600001", "lb_days": 1}),
    }})
    result, clients, _ = _run(
        None, td_rows={"d_600001": [_daily_row()]}, redis=legacy,
    )
    assert clients == []
    assert result["core_count"] == 1
    assert result["seal_evidence_mode"] == "NONE"
    assert result["core_no_seal_evidence_count"] == 1
    assert result["excluded_limit_price_without_seal"] == 0
    assert result["legacy_count"] == 1
    assert result["both"] == 1
    assert result["core_only"] == []
    assert result["legacy_only"] == []
    assert result["lb_diff"] == []
    assert result["write"]["status"] == "DRY_RUN"
    assert result["write"]["key"] == "core:cache:yest_limit_pool:2026-10-13"


def test_apply_without_confirmation_returns_code_2_without_writing():
    from examples.run_core_limit_up_pool_cache import main

    with pytest.raises(SystemExit) as error:
        main(["--trade-date", "2026-10-13", "--previous-trade-date", "2026-10-12",
              "--universe-symbols-file", "unused", "--output", "/tmp/c051-should-not-exist.json",
              "--apply"])
    assert error.value.code == 2


def test_confirmed_write_uses_only_core_cache_keys_and_records_meta():
    result, clients, _ = _run(
        None, td_rows={"d_600001": [_daily_row()]}, apply=True, confirm=True,
    )
    assert len(clients) == 1
    keys = [call[1] for call in clients[0].calls]
    assert all(key.startswith("core:cache:") for key in keys)
    assert result["write"]["status"] == "WRITTEN"
    assert result["write"]["rows"] == 1
    assert result["write"]["ttl_seconds"] == 3 * 86400
    assert result["write"]["meta_key"] == "core:cache:meta:yest_limit_pool:2026-10-13"
    assert result["write"]["meta"]["source"] == "core_td_kline;name_fallback;no_seal_evidence"


def test_missing_td_tables_are_counted_without_discarding_available_symbols():
    result, _, _ = _run(None, td_rows={"d_600001": [_daily_row()]})
    assert result["missing_table_symbols"] == ["600002"]
    assert result["core_count"] == 1


def test_empty_derivation_does_not_write_or_replace_previous_cache():
    result, clients, _ = _run(
        None, td_rows={"d_600001": [], "d_600002": []},
        apply=True, confirm=True,
    )
    assert result["core_count"] == 0
    assert result["write"]["status"] == "EMPTY_ROWS_PRESERVES_PREVIOUS"
    assert clients == []


def test_eod_capture_loads_integer_ls_only_from_q2_hash_reads(tmp_path):
    capture = tmp_path / "eod.json"
    capture.write_text(json.dumps({
        "contract_version": "RedisQ2ReadCaptureV1",
        "reads": [
            {"operation": "hgetall", "key": "q2:600001", "value": {"ls": "1"}},
            {"operation": "hgetall", "key": "q2:600002", "value": {"ls": "bad"}},
            {"operation": "hgetall", "key": "q2:active:20261012", "value": {"ls": "1"}},
        ],
    }), encoding="utf-8")
    assert _load_eod_capture(capture) == {"600001": 1}
