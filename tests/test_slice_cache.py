from engine_core.slice_cache import (build_daily_history_slice,
                                     load_daily_history_slice,
                                     write_daily_history_slice)


def test_daily_history_slice_round_trips_rows_and_manifest(tmp_path):
    rows = [
        {"date": "2026-10-15", "symbol": "1", "open": 10, "high": 12,
         "low": 9, "close": 11, "pct_chg": 10, "amount_yuan": 1000},
        {"date": "2026-10-16", "symbol": "1", "open": 11, "high": 11,
         "low": 11, "close": 11, "pct_chg": 0, "amount_yuan": 0},
    ]
    slice_data = build_daily_history_slice(rows, trade_date="2026-10-16",
                                           source_sha256="source-hash", asof="2026-10-16T08:50:00")
    target = write_daily_history_slice(tmp_path, "2026-10-16", slice_data)
    assert target.name == "daily_history_compact.json.gz"
    loaded, manifest = load_daily_history_slice(tmp_path, "2026-10-16")
    assert target.is_file()
    assert loaded == [{**rows[0], "symbol": "000001", "open": 10.0, "high": 12.0,
                       "low": 9.0, "close": 11.0, "pct_chg": 10.0, "amount_yuan": 1000.0}]
    assert manifest["source_sha256"] == "source-hash"
    assert manifest["asof"] == "2026-10-16T08:50:00"
    assert manifest["schema"] == "DailyHistoryCompactV1"


def test_daily_history_slice_missing_returns_none(tmp_path):
    assert load_daily_history_slice(tmp_path, "2026-10-16") == (None, None)


def test_daily_history_slice_rejects_corrupt_payload(tmp_path):
    rows = [{"date": "2026-10-15", "symbol": "1", "close": 11}]
    data = build_daily_history_slice(rows, trade_date="2026-10-16",
                                     source_sha256="source-hash", asof="2026-10-16T08:50:00")
    path = write_daily_history_slice(tmp_path, "2026-10-16", data)
    path.write_bytes(b"corrupt")
    assert load_daily_history_slice(tmp_path, "2026-10-16") == (None, None)


def test_history_loader_uses_source_verified_cache_and_excludes_future_rows(tmp_path, monkeypatch, capsys):
    from engine_core.live_session import load_daily_history_csv
    from engine_core.slice_cache import sha256_file

    source = tmp_path / "daily.csv"
    source.write_text("date,symbol,open,high,low,close,pct_chg,amount_yuan\n"
                      "2026-10-15,1,10,12,9,11,10,1000\n"
                      "2026-10-16,1,11,11,11,11,0,0\n", encoding="utf-8")
    rows = load_daily_history_csv(source)
    data = build_daily_history_slice(rows, trade_date="2026-10-16",
                                     source_sha256=sha256_file(source), asof="2026-10-16T08:50:00")
    write_daily_history_slice(tmp_path / "slices", "2026-10-16", data)
    monkeypatch.setenv("CORE_SLICE_ROOT", str(tmp_path / "slices"))
    loaded = load_daily_history_csv(source, trade_date="2026-10-16")
    assert len(loaded) == 1 and loaded[0]["date"] == "2026-10-15"
    assert "SLICE_MISSING_FALLBACK" not in capsys.readouterr().err


def test_history_loader_missing_slice_falls_back_with_warning(tmp_path, monkeypatch, capsys):
    from engine_core.live_session import load_daily_history_csv

    source = tmp_path / "daily.csv"
    source.write_text("date,symbol,close\n2026-10-15,1,11\n", encoding="utf-8")
    monkeypatch.setenv("CORE_SLICE_ROOT", str(tmp_path / "missing"))
    rows = load_daily_history_csv(source, trade_date="2026-10-16")
    assert rows[0]["symbol"] == "000001" and rows[0]["close"] == 11
    assert "SLICE_MISSING_FALLBACK daily_history trade_date=2026-10-16" in capsys.readouterr().err

