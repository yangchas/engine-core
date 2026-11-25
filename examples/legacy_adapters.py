"""Thin, lazy adapters for optional legacy system integrations."""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from typing import Any, Callable


def _insert_legacy_root(legacy_root: str | Path) -> None:
    sys.path.insert(0, str(legacy_root))


def wencai_fetch_callable(legacy_root: str | Path) -> Callable[[str, bool, int], Any]:
    """Build the synchronous WenCai fetch callable used by command line tools."""
    _insert_legacy_root(legacy_root)
    from ai.API.api import UnifiedMarketDataFetcher  # type: ignore[import-not-found]

    fetcher = UnifiedMarketDataFetcher()

    def fetch(query: str, loop: bool, max_stocks: int) -> Any:
        return asyncio.run(fetcher._get_wencai_stocks(
            query, loop=loop, max_stocks=max_stocks, return_df=True,
        ))

    def redaction_values() -> set[str]:
        cookie = getattr(fetcher, "wencai_cookie", "")
        if not isinstance(cookie, str):
            return set()
        return {
            cookie[start:end]
            for start in range(len(cookie))
            for end in range(start + 1, len(cookie) + 1)
        }

    fetch.redaction_values = redaction_values  # type: ignore[attr-defined]
    return fetch


def kaipan_analyzer(legacy_root: str | Path) -> Any:
    _insert_legacy_root(legacy_root)
    from ai.API.StockAnalyzer import StockAnalyzer  # type: ignore[import-not-found]

    return StockAnalyzer()


def kaipan_plate_members_callable(legacy_root: str | Path) -> Callable[[str, str], Any]:
    """Build a thin callable for date-specific Kaipan plate membership."""
    _insert_legacy_root(legacy_root)
    kaipan = kaipan_pykaipan_module()

    def fetch(plate_code: str, trade_date: str) -> Any:
        return kaipan.getHisPlateIds(
            p=plate_code, date=trade_date, index="0", size="2000",
        )

    def redaction_values() -> set[str]:
        config = getattr(kaipan, "config", {})
        if not isinstance(config, dict):
            return set()
        return {value for value in config.values() if isinstance(value, str) and value}

    fetch.redaction_values = redaction_values  # type: ignore[attr-defined]
    return fetch


def kaipan_pykaipan_module() -> Any:
    return importlib.import_module("pykaipan.pykaipan")
