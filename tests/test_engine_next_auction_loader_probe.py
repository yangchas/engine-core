from types import SimpleNamespace

import pytest

from examples.run_engine_next_auction_loader_probe import (
    _select_rows,
    _strict_symbols,
    _strict_tags,
    _summarize,
)


def test_loader_probe_validates_and_deduplicates_symbols():
    assert _strict_symbols("600519,000001,600519") == ("000001", "600519")
    with pytest.raises(ValueError):
        _strict_symbols("600519,60051X")


def test_loader_probe_validates_tags_and_preserves_first_order():
    assert _strict_tags("0924,0920,0924") == ("0924", "0920")
    with pytest.raises(ValueError):
        _strict_tags("0925,latest")


def test_loader_probe_selects_requested_rows_in_stable_order():
    rows = [
        {"tag": "0924", "symbol": "600519", "amount": 2},
        {"tag": "0920", "symbol": "000001", "amount": 1},
        {"tag": "0920", "symbol": "600519", "amount": 3},
    ]
    assert _select_rows(rows, ("600519", "000001")) == [
        {"tag": "0920", "symbol": "000001", "amount": 1},
        {"tag": "0920", "symbol": "600519", "amount": 3},
        {"tag": "0924", "symbol": "600519", "amount": 2},
    ]


def test_loader_probe_preserves_legacy_key_evidence_without_claiming_writes():
    result = _summarize(
        SimpleNamespace(
            rows=[{"tag": "0925", "symbol": "600519"}],
            source="redis_snapshots",
            notes=("read",),
            redis_keys_written=("market:auction:20260911:0925",),
        ),
        trade_date="2026-09-11",
        tags=("0920", "0924", "0925"),
        symbols=("600519",),
        writes=(),
    )
    assert result["read_only"] is True
    assert result["legacy_keys_reported"] == ("market:auction:20260911:0925",)
    assert result["guard_writes"] == ()
    assert result["row_count_by_tag"] == {"0920": 0, "0924": 0, "0925": 1}
    assert result["duplicate_row_keys"] == []


def test_loader_probe_surfaces_duplicate_tag_symbol_rows():
    result = _summarize(
        SimpleNamespace(
            rows=[
                {"tag": "0925", "symbol": "600519"},
                {"tag": "0925", "symbol": "600519"},
            ],
            source="redis_snapshots",
            notes=(),
            redis_keys_written=(),
        ),
        trade_date="2026-09-13",
        tags=("0925",),
        symbols=("600519",),
        writes=(),
    )
    assert result["duplicate_row_keys"] == [("0925", "600519")]
