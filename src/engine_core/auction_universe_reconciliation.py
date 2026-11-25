"""Read-only, fact-only comparison of an auction anchor and TD cohort.

This module accepts already observed data and performs no Redis/TD/Rabbit I/O.
Universe differences are visible diagnostics: they never reject or suppress a
downstream Core result. It deliberately does not infer that zero-valued rows
are inactive securities.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import date
from typing import Any, Iterable, Mapping

from .contracts import canonical_hash, canonical_json


AUCTION_UNIVERSE_RECONCILIATION_CONTRACT = "AuctionUniverseReconciliationV1"
_DATE_RE = re.compile(r"(?:\d{4}-\d{2}-\d{2}|\d{8})\Z")
_TAG_RE = re.compile(r"\d{4}\Z")
_SYMBOL_RE = re.compile(r"\d{6}\Z")
_TD_VALUE_FIELDS = (
    "match_amt_yuan",
    "rest_bid_amt_yuan",
    "rest_ask_amt_yuan",
    "chg_bp",
)


def _trade_date(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be YYYYMMDD or YYYY-MM-DD")
    text = value.strip()
    if _DATE_RE.fullmatch(text) is None:
        raise ValueError(f"{field} must be YYYYMMDD or YYYY-MM-DD")
    compact = text.replace("-", "")
    try:
        parsed = date(int(compact[:4]), int(compact[4:6]), int(compact[6:8]))
    except ValueError as exc:
        raise ValueError(f"{field} must be a valid trade date") from exc
    return parsed.strftime("%Y%m%d")


def _symbol(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a six-character symbol string")
    text = value.strip()
    if _SYMBOL_RE.fullmatch(text) is None:
        raise ValueError(f"{field} must be a six-digit symbol")
    return text


def _integer_or_null(value: Any, *, field: str, nonnegative: bool) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer or null")
    if nonnegative and value < 0:
        raise ValueError(f"{field} must be nonnegative")
    return value


def _td_only_profile(rows: list[dict[str, Any]]) -> str:
    profiles: set[str] = set()
    for row in rows:
        amounts = (
            row["match_amt_yuan"],
            row["rest_bid_amt_yuan"],
            row["rest_ask_amt_yuan"],
        )
        if any(value is None for value in amounts):
            profiles.add("UNKNOWN_FIELDS")
        elif amounts == (0, 0, 0) and row["chg_bp"] is None:
            profiles.add("ZERO_AMOUNT_NULL_CHANGE")
        else:
            profiles.add("OTHER")
    if len(profiles) > 1:
        return "DUPLICATE_CONFLICT"
    return next(iter(profiles))


def _limited(values: list[Any], limit: int) -> tuple[list[Any], bool]:
    return values[:limit], len(values) > limit


def attribute_opening_stale_symbols(
    facts_by_symbol: Mapping[str, Mapping[str, Any]],
    td_rows: Iterable[Mapping[str, Any]],
    *,
    trade_date: str,
    auction_tag: str,
    evaluation_time_ms: int,
    stale_after_ms: int,
    anchor_snapshot: Mapping[str, Any] | None = None,
    detail_limit: int = 100,
) -> dict[str, Any]:
    """Describe same-day TD/anchor observations for stale opening facts.

    This is a report-only association, not a stale cause classifier. It does
    not infer that a TD zero/null row means no activity, and it never changes
    the supplied facts or downstream readiness. Missing timestamps remain
    unknown; future timestamps are reported separately from fresh facts.
    """

    normalized_date = _trade_date(trade_date, field="trade_date")
    if not isinstance(auction_tag, str) or _TAG_RE.fullmatch(auction_tag) is None:
        raise ValueError("auction_tag must be a four-digit string")
    if (
        isinstance(evaluation_time_ms, bool)
        or not isinstance(evaluation_time_ms, int)
        or evaluation_time_ms < 0
    ):
        raise ValueError("evaluation_time_ms must be a nonnegative integer")
    if (
        isinstance(stale_after_ms, bool)
        or not isinstance(stale_after_ms, int)
        or stale_after_ms < 0
    ):
        raise ValueError("stale_after_ms must be a nonnegative integer")
    if isinstance(detail_limit, bool) or not isinstance(detail_limit, int):
        raise ValueError("detail_limit must be an integer")
    if detail_limit < 0:
        raise ValueError("detail_limit must be nonnegative")
    if not isinstance(facts_by_symbol, Mapping):
        raise ValueError("facts_by_symbol must be a mapping")

    normalized_facts: dict[str, dict[str, Any]] = {}
    timestamp_status_by_symbol: dict[str, dict[str, Any]] = {}
    for raw_symbol, raw_fact in facts_by_symbol.items():
        symbol = _symbol(raw_symbol, field="fact symbol key")
        if symbol in normalized_facts:
            raise ValueError("facts_by_symbol contains duplicate normalized symbols")
        if not isinstance(raw_fact, Mapping):
            raise ValueError(f"fact {symbol} must be a mapping")
        if "symbol" in raw_fact and _symbol(
            raw_fact["symbol"], field=f"fact {symbol} symbol"
        ) != symbol:
            raise ValueError(f"fact {symbol} symbol does not match its mapping key")
        timestamp_ms = raw_fact.get("timestamp_ms")
        if timestamp_ms is None:
            normalized_facts[symbol] = {"timestamp_ms": None}
            timestamp_status_by_symbol[symbol] = {
                "timestamp_ms": None,
                "age_ms": None,
                "status": "UNKNOWN_TIMESTAMP",
            }
            continue
        if (
            isinstance(timestamp_ms, bool)
            or not isinstance(timestamp_ms, int)
            or timestamp_ms < 0
        ):
            raise ValueError(f"fact {symbol} timestamp_ms must be a nonnegative integer or null")
        age_ms = evaluation_time_ms - timestamp_ms
        normalized_facts[symbol] = {"timestamp_ms": timestamp_ms}
        timestamp_status_by_symbol[symbol] = {
            "timestamp_ms": timestamp_ms,
            "age_ms": age_ms,
            "status": (
                "STALE"
                if age_ms > stale_after_ms
                else "FUTURE_TIMESTAMP"
                if age_ms < 0
                else "FRESH"
            ),
        }

    if anchor_snapshot is not None:
        if not isinstance(anchor_snapshot, Mapping):
            raise ValueError("anchor_snapshot must be a mapping or null")
        try:
            anchor_date = _trade_date(
                anchor_snapshot["trade_date"], field="anchor trade_date"
            )
            anchor_tag = anchor_snapshot["auction_tag"]
            raw_anchor_symbols = anchor_snapshot["symbols"]
        except KeyError as exc:
            raise ValueError(f"anchor snapshot missing {exc.args[0]}") from exc
        if anchor_date != normalized_date:
            raise ValueError("anchor snapshot trade_date does not match input")
        if anchor_tag != auction_tag:
            raise ValueError("anchor snapshot auction_tag does not match input")
        if isinstance(raw_anchor_symbols, (str, bytes, Mapping)):
            raise ValueError("anchor symbols must be an iterable of symbol strings")
        try:
            normalized_anchor_members = [
                _symbol(value, field="anchor symbol") for value in raw_anchor_symbols
            ]
        except TypeError as exc:
            raise ValueError("anchor symbols must be iterable") from exc
        anchor_symbols = set(normalized_anchor_members)
    else:
        normalized_anchor_members = []
        anchor_symbols = None

    normalized_td_rows: list[dict[str, Any]] = []
    td_rows_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for index, raw_row in enumerate(td_rows):
        if not isinstance(raw_row, Mapping):
            raise ValueError(f"TD row {index} must be a mapping")
        try:
            row_date = _trade_date(
                raw_row["trade_date"], field=f"TD row {index} trade_date"
            )
            row_tag = raw_row["auction_tag"]
            symbol = _symbol(raw_row["symbol"], field=f"TD row {index} symbol")
            row_values = {
                field: _integer_or_null(
                    raw_row[field],
                    field=f"TD row {index} {field}",
                    nonnegative=field != "chg_bp",
                )
                for field in _TD_VALUE_FIELDS
            }
        except KeyError as exc:
            raise ValueError(f"TD row {index} missing {exc.args[0]}") from exc
        if row_date != normalized_date:
            raise ValueError(f"TD row {index} trade_date does not match input")
        if not isinstance(row_tag, str) or row_tag != auction_tag:
            raise ValueError(f"TD row {index} auction_tag does not match input")
        row = {
            "trade_date": row_date,
            "auction_tag": row_tag,
            "symbol": symbol,
            **row_values,
        }
        normalized_td_rows.append(row)
        td_rows_by_symbol[symbol].append(row)

    freshness_by_symbol = timestamp_status_by_symbol
    stale_symbols = sorted(
        symbol
        for symbol, freshness in freshness_by_symbol.items()
        if freshness["status"] == "STALE"
    )
    td_symbol_set = set(td_rows_by_symbol)
    td_only_symbols = (
        sorted(td_symbol_set - anchor_symbols) if anchor_symbols is not None else []
    )

    by_symbol: dict[str, dict[str, Any]] = {}
    td_row_status_counts: Counter[str] = Counter()
    anchor_status_counts: Counter[str] = Counter()
    for symbol in stale_symbols:
        rows = td_rows_by_symbol.get(symbol, [])
        profile = _td_only_profile(rows) if rows else None
        td_status = (
            "TD_ROW_ABSENT"
            if not rows
            else "TD_ROW_ZERO_AMOUNT_NULL_CHANGE"
            if profile == "ZERO_AMOUNT_NULL_CHANGE"
            else "TD_ROW_PRESENT_OTHER"
        )
        anchor_status = (
            "ANCHOR_NOT_PROVIDED"
            if anchor_symbols is None
            else "IN_ANCHOR"
            if symbol in anchor_symbols
            else "NOT_IN_ANCHOR"
        )
        freshness = freshness_by_symbol[symbol]
        by_symbol[symbol] = {
            "symbol": symbol,
            "timestamp_ms": freshness["timestamp_ms"],
            "age_ms": freshness["age_ms"],
            "td_row_status": td_status,
            "td_row_profile": profile,
            "anchor_status": anchor_status,
        }
        td_row_status_counts[td_status] += 1
        anchor_status_counts[anchor_status] += 1

    nonstale_zero_symbols: list[str] = []
    nonstale_zero_unknown_timestamp_count = 0
    if anchor_symbols is not None:
        for symbol in td_only_symbols:
            if _td_only_profile(td_rows_by_symbol[symbol]) != "ZERO_AMOUNT_NULL_CHANGE":
                continue
            freshness = freshness_by_symbol.get(symbol)
            if freshness is None:
                continue
            if freshness["status"] == "UNKNOWN_TIMESTAMP":
                nonstale_zero_unknown_timestamp_count += 1
            elif freshness["status"] == "FRESH":
                nonstale_zero_symbols.append(symbol)

    shown_symbols = stale_symbols[:detail_limit]
    shown_nonstale_zero_symbols = nonstale_zero_symbols[:detail_limit]
    normalized_td_rows.sort(key=canonical_json)
    input_hash = canonical_hash(
        {
            "contract": "OpeningStaleAuctionUniverseAttributionInputV1",
            "trade_date": normalized_date,
            "auction_tag": auction_tag,
            "evaluation_time_ms": evaluation_time_ms,
            "stale_after_ms": stale_after_ms,
            "facts_by_symbol": [
                {
                    "symbol": symbol,
                    **normalized_facts[symbol],
                    "freshness": freshness_by_symbol[symbol]["status"],
                    "age_ms": freshness_by_symbol[symbol]["age_ms"],
                }
                for symbol in sorted(normalized_facts)
            ],
            "anchor_symbols": (
                sorted(normalized_anchor_members)
                if anchor_symbols is not None
                else None
            ),
            "td_rows": normalized_td_rows,
        }
    )
    semantic_result = {
        "contract": "OpeningStaleAuctionUniverseAttributionV1",
        "trade_date": normalized_date,
        "auction_tag": auction_tag,
        "evaluation_time_ms": evaluation_time_ms,
        "stale_after_ms": stale_after_ms,
        "scope": "PROVIDED_INPUTS_ONLY",
        "stale_symbol_count": len(stale_symbols),
        "unknown_timestamp_count": sum(
            status["status"] == "UNKNOWN_TIMESTAMP"
            for status in freshness_by_symbol.values()
        ),
        "future_timestamp_count": sum(
            status["status"] == "FUTURE_TIMESTAMP"
            for status in freshness_by_symbol.values()
        ),
        "td_row_status_counts": dict(sorted(td_row_status_counts.items())),
        "anchor_status_counts": dict(sorted(anchor_status_counts.items())),
        "td_only_zero_amount_null_change_not_stale_count": len(
            nonstale_zero_symbols
        ),
        "td_only_zero_amount_null_change_unknown_timestamp_count": (
            nonstale_zero_unknown_timestamp_count
        ),
        "by_symbol": [by_symbol[symbol] for symbol in stale_symbols],
        "input_content_hash": input_hash,
        "causal_attribution": "NOT_ESTABLISHED",
    }
    content_hash = canonical_hash(semantic_result)
    return {
        **semantic_result,
        "by_symbol": {symbol: by_symbol[symbol] for symbol in shown_symbols},
        "by_symbol_truncated": len(shown_symbols) < len(stale_symbols),
        "td_only_zero_amount_null_change_not_stale_symbols": (
            shown_nonstale_zero_symbols
        ),
        "td_only_zero_amount_null_change_not_stale_symbols_truncated": (
            len(shown_nonstale_zero_symbols) < len(nonstale_zero_symbols)
        ),
        "diagnostic_only": True,
        "blocks_downstream": False,
        "content_hash": content_hash,
    }


def reconcile_auction_universe(
    anchor_snapshot: Mapping[str, Any],
    td_rows: Iterable[Mapping[str, Any]],
    *,
    detail_limit: int = 100,
) -> dict[str, Any]:
    """Return a deterministic, non-blocking 09:xx anchor/TD universe audit.

    ``anchor_snapshot`` is the small adapter envelope ``{trade_date,
    auction_tag, symbols}``; it does not assume ownership of Redis payload
    decoding. Each TD row must carry ``trade_date``, ``auction_tag``,
    ``symbol``, and the four typed fields used for the descriptive
    TD-only-row profile. Amount fields are integer yuan; ``chg_bp`` is signed
    integer basis points. A missing amount value remains unknown, never zero.

    Dates and tags must agree across both sources. Malformed identifiers or
    missing/wrongly typed schema fields raise ``ValueError``. An empty side is
    ``NOT_COMPARABLE``. A non-empty set difference is only
    ``MISMATCH_VISIBLE``; ``blocks_downstream`` is always false.
    """

    if not isinstance(anchor_snapshot, Mapping):
        raise ValueError("anchor_snapshot must be a mapping")
    if isinstance(detail_limit, bool) or not isinstance(detail_limit, int):
        raise ValueError("detail_limit must be an integer")
    if detail_limit < 0:
        raise ValueError("detail_limit must be nonnegative")

    try:
        trade_date = _trade_date(anchor_snapshot["trade_date"], field="anchor trade_date")
        auction_tag = anchor_snapshot["auction_tag"]
        raw_anchor_symbols = anchor_snapshot["symbols"]
    except KeyError as exc:
        raise ValueError(f"anchor snapshot missing {exc.args[0]}") from exc
    if not isinstance(auction_tag, str) or _TAG_RE.fullmatch(auction_tag) is None:
        raise ValueError("anchor auction_tag must be a four-digit string")
    if isinstance(raw_anchor_symbols, (str, bytes, Mapping)):
        raise ValueError("anchor symbols must be an iterable of symbol strings")
    try:
        anchor_input_symbols = [
            _symbol(item, field="anchor symbol") for item in raw_anchor_symbols
        ]
    except TypeError as exc:
        raise ValueError("anchor symbols must be iterable") from exc

    td_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    canonical_td_rows: list[dict[str, Any]] = []
    for index, raw_row in enumerate(td_rows):
        if not isinstance(raw_row, Mapping):
            raise ValueError(f"TD row {index} must be a mapping")
        try:
            row_date = _trade_date(raw_row["trade_date"], field=f"TD row {index} trade_date")
            row_tag = raw_row["auction_tag"]
            symbol = _symbol(raw_row["symbol"], field=f"TD row {index} symbol")
            values = {
                "match_amt_yuan": _integer_or_null(
                    raw_row["match_amt_yuan"],
                    field=f"TD row {index} match_amt_yuan",
                    nonnegative=True,
                ),
                "rest_bid_amt_yuan": _integer_or_null(
                    raw_row["rest_bid_amt_yuan"],
                    field=f"TD row {index} rest_bid_amt_yuan",
                    nonnegative=True,
                ),
                "rest_ask_amt_yuan": _integer_or_null(
                    raw_row["rest_ask_amt_yuan"],
                    field=f"TD row {index} rest_ask_amt_yuan",
                    nonnegative=True,
                ),
                "chg_bp": _integer_or_null(
                    raw_row["chg_bp"],
                    field=f"TD row {index} chg_bp",
                    nonnegative=False,
                ),
            }
        except KeyError as exc:
            raise ValueError(f"TD row {index} missing {exc.args[0]}") from exc
        if row_date != trade_date:
            raise ValueError(f"TD row {index} trade_date does not match anchor")
        if not isinstance(row_tag, str) or row_tag != auction_tag:
            raise ValueError(f"TD row {index} auction_tag does not match anchor")
        row = {
            "trade_date": row_date,
            "auction_tag": row_tag,
            "symbol": symbol,
            **values,
        }
        td_by_symbol[symbol].append(row)
        canonical_td_rows.append(row)

    anchor_symbols = set(anchor_input_symbols)
    td_symbols = set(td_by_symbol)
    shared = sorted(anchor_symbols & td_symbols)
    anchor_only = sorted(anchor_symbols - td_symbols)
    td_only = sorted(td_symbols - anchor_symbols)
    duplicate_anchor_symbols = len(anchor_input_symbols) - len(anchor_symbols)
    duplicate_td_symbols = sorted(
        symbol for symbol, rows in td_by_symbol.items() if len(rows) > 1
    )
    duplicate_td_row_count = len(canonical_td_rows) - len(td_symbols)

    if not anchor_symbols or not td_symbols:
        status = "NOT_COMPARABLE"
    elif anchor_only or td_only:
        status = "MISMATCH_VISIBLE"
    else:
        status = "MATCH"

    td_only_profiles = {
        symbol: _td_only_profile(td_by_symbol[symbol]) for symbol in td_only
    }
    profile_counts = dict(sorted(Counter(td_only_profiles.values()).items()))
    counts = {
        "anchor_symbol_count": len(anchor_symbols),
        "anchor_duplicate_symbol_count": duplicate_anchor_symbols,
        "td_row_count": len(canonical_td_rows),
        "td_unique_symbol_count": len(td_symbols),
        "td_duplicate_row_count": duplicate_td_row_count,
        "shared_symbol_count": len(shared),
        "anchor_only_count": len(anchor_only),
        "td_only_count": len(td_only),
    }

    canonical_td_rows.sort(key=canonical_json)
    input_content_hash = canonical_hash(
        {
            "contract": AUCTION_UNIVERSE_RECONCILIATION_CONTRACT,
            "trade_date": trade_date,
            "auction_tag": auction_tag,
            "anchor_symbols": sorted(anchor_input_symbols),
            "td_rows": canonical_td_rows,
        }
    )
    semantic_result = {
        "contract": AUCTION_UNIVERSE_RECONCILIATION_CONTRACT,
        "trade_date": trade_date,
        "auction_tag": auction_tag,
        "status": status,
        "counts": counts,
        "td_only_classification_counts": profile_counts,
        "anchor_only_symbols": anchor_only,
        "td_only_symbols": td_only,
        "td_only_classification_by_symbol": td_only_profiles,
        "duplicate_td_symbols": duplicate_td_symbols,
        "input_content_hash": input_content_hash,
    }
    content_hash = canonical_hash(semantic_result)

    shown_anchor_only, anchor_only_truncated = _limited(anchor_only, detail_limit)
    shown_td_only, td_only_truncated = _limited(td_only, detail_limit)
    shown_profiles, profile_truncated = _limited(
        [
            {"symbol": symbol, "classification": td_only_profiles[symbol]}
            for symbol in td_only
        ],
        detail_limit,
    )
    shown_duplicates, duplicates_truncated = _limited(
        duplicate_td_symbols, detail_limit
    )
    return {
        "contract": AUCTION_UNIVERSE_RECONCILIATION_CONTRACT,
        "trade_date": trade_date,
        "auction_tag": auction_tag,
        "status": status,
        "counts": counts,
        "td_only_classification_counts": profile_counts,
        "anchor_only_symbols": shown_anchor_only,
        "anchor_only_symbols_truncated": anchor_only_truncated,
        "td_only_symbols": shown_td_only,
        "td_only_symbols_truncated": td_only_truncated,
        "td_only_classification_by_symbol": shown_profiles,
        "td_only_classification_by_symbol_truncated": profile_truncated,
        "duplicate_td_symbols": shown_duplicates,
        "duplicate_td_symbols_truncated": duplicates_truncated,
        "details_valid": status != "NOT_COMPARABLE",
        "diagnostic_only": True,
        "blocks_downstream": False,
        "input_content_hash": input_content_hash,
        "content_hash": content_hash,
    }
