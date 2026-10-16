"""Point-in-time fallback chains for auction and limit-state slices.

Network clients, Redis, and TD readers are injected. This module performs no
connection setup and never writes to Redis or TD.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from ..data_pipeline.providers.market_fallback_chain import WencaiMarketGate
from ..limit_price import is_limit_down, is_limit_up, limit_prices, touched_limit, touched_limit_down
from . import SliceRequest, SliceResult


Source = Callable[[SliceRequest], Mapping[str, Any] | None]
SHANGHAI = ZoneInfo("Asia/Shanghai")


def _cutoff(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("slice_asof_timezone_required")
    return parsed


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except ValueError:
        return None


def _visible(payload: Mapping[str, Any], request: SliceRequest) -> tuple[list[dict[str, Any]], bool]:
    cutoff = _cutoff(request.asof)
    payload_available = _parse_dt(payload.get("available_at"))
    if payload_available and payload_available > cutoff:
        return [], True
    selected: list[dict[str, Any]] = []
    filtered = False
    for raw in payload.get("rows", ()) or ():
        if not isinstance(raw, Mapping):
            continue
        row = dict(raw)
        event = _parse_dt(row.get("event_time") or row.get("source_event_at"))
        available = _parse_dt(row.get("available_at") or row.get("observed_at")) or payload_available
        if event and event > cutoff or available and available > cutoff:
            filtered = True
            continue
        if available is not None:
            row["available_at"] = available.isoformat()
        elif payload_available is None:
            row["pit_unknown"] = True
        selected.append(row)
    return selected, filtered


def _coverage(rows: list[dict[str, Any]], expected: Any, slice_id: str) -> tuple[float | None, int]:
    count = int(expected) if isinstance(expected, int) and expected >= 0 else None
    if slice_id == "auction_snapshot":
        fields = ("price_milli", "match_price_milli", "match_price", "match_amount_yuan",
                  "change_pct", "change_pct_ratio")
    else:
        fields = ("limit_state", "limit_up_price", "limit_down_price", "at_limit_up_price",
                  "at_limit_down_price", "limit_up_today", "wencai_in_limit_up_pool")
    usable = {str(row.get("symbol", "")).zfill(6) for row in rows
              if row.get("symbol") and any(row.get(field) is not None for field in fields)}
    if count is None:
        return (1.0 if usable else None), len(rows)
    return (len(usable) / count if count else (1.0 if not rows else None)), count


def _number(value: Any) -> float | None:
    if value in (None, "", "--", "-") or isinstance(value, bool):
        return None
    try:
        parsed = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None


class MarketFallbackProviders:
    """Create singleflight-compatible slice providers with source lineage."""

    def __init__(self, *, auction_td: Source | None = None,
                 auction_redis: Source | None = None, auction_wencai: Source | None = None,
                 limit_inputs: Source | None = None, limit_q2: Source | None = None,
                 limit_wencai: Source | None = None, redis: Any = None,
                 wencai_gate: WencaiMarketGate | None = None,
                 coverage_threshold: float = 0.95):
        if not 0 < coverage_threshold <= 1:
            raise ValueError("coverage_threshold_out_of_range")
        self.redis = redis
        self.auction_td = auction_td
        self.auction_redis = auction_redis or (self._redis_auction if redis is not None else None)
        self.auction_wencai = auction_wencai or (self._wencai_auction if wencai_gate else None)
        self.limit_inputs, self.limit_q2 = limit_inputs, limit_q2
        self.limit_wencai = limit_wencai or (self._wencai_limit if wencai_gate else None)
        self.wencai_gate = wencai_gate
        self.coverage_threshold = coverage_threshold

    def providers(self) -> dict[str, list[Callable[..., SliceResult]]]:
        return {name: [self._resolve] for name in
                ("auction_snapshot", "limit_state", "limit_up_today")}

    def _resolve(self, _reader: Any, request: SliceRequest) -> SliceResult:
        if request.slice_id == "auction_snapshot":
            candidates = [("td_auction_snapshot_v2", self.auction_td, False),
                          ("legacy_redis_q2", self.auction_redis, True),
                          ("wencai", self.auction_wencai, True)]
        else:
            candidates = [("price_derived", self._derive_limit_inputs, False),
                          ("legacy_redis_q2", self.limit_q2 or self._redis_q2_limit, True),
                          ("wencai", self.limit_wencai, True)]

        lineage: list[dict[str, Any]] = []
        best: tuple[str, list[dict[str, Any]], Mapping[str, Any], bool, float | None] | None = None
        fallback_reason = "PRIMARY_SOURCE_MISSING"
        for source, provider, degraded in candidates:
            if provider is None:
                lineage.append({"source": source, "status": "NOT_CONFIGURED", "coverage": None})
                continue
            try:
                payload = provider(request)
                if not isinstance(payload, Mapping):
                    payload = {"rows": []}
                rows, future_filtered = _visible(payload, request)
                expected = payload.get("expected_count", request.params.get("expected_count"))
                coverage, expected_count = _coverage(rows, expected, request.slice_id)
                attempt = {"source": source, "status": payload.get("status", "AVAILABLE"),
                           "coverage": coverage, "row_count": len(rows),
                           "expected_count": expected_count,
                           "available_at": payload.get("available_at"),
                           "reason": payload.get("reason"),
                           "partial": payload.get("partial", not payload.get("complete", True))}
                if payload.get("query_log") is not None:
                    attempt["query_log"] = payload["query_log"]
                if payload.get("labels") is not None:
                    attempt["labels"] = payload["labels"]
                if future_filtered:
                    attempt["future_rows_filtered"] = True
                lineage.append(attempt)
                if rows and (best is None or (coverage or 0) > (best[4] or 0)):
                    best = (source, rows, payload, degraded, coverage)
                adequate = bool(rows) and (coverage is None or coverage >= self.coverage_threshold)
                if adequate:
                    return self._result(request, source, rows, payload, lineage,
                                        degraded=degraded, coverage=coverage,
                                        future_filtered=future_filtered,
                                        degraded_reason=fallback_reason if degraded else None)
                if source == "td_auction_snapshot_v2" and rows:
                    fallback_reason = "TD_COVERAGE_BELOW_THRESHOLD"
                elif not rows and source == "td_auction_snapshot_v2":
                    fallback_reason = str(payload.get("reason") or "TD_SOURCE_MISSING")
            except Exception as exc:
                lineage.append({"source": source, "status": "ERROR",
                                "reason": f"{type(exc).__name__}"})
                if source == "td_auction_snapshot_v2":
                    fallback_reason = "TD_SOURCE_ERROR"

        if best:
            source, rows, payload, degraded, coverage = best
            return self._result(request, source, rows, payload, lineage,
                                degraded=source != "td_auction_snapshot_v2" and source != "price_derived",
                                coverage=coverage, degraded_reason=fallback_reason)
        return SliceResult.create([], {
            "slice_id": request.slice_id, "trade_date": request.trade_date,
            "asof": request.asof, "source": "none", "status": "MISSING",
            "reason": "NO_PROVIDER_DATA", "quality": {"coverage": None, "partial": True},
            "lineage": lineage,
        })

    def _result(self, request: SliceRequest, source: str, rows: list[dict[str, Any]],
                payload: Mapping[str, Any], lineage: list[dict[str, Any]], *, degraded: bool,
                coverage: float | None, future_filtered: bool = False,
                degraded_reason: str | None = None) -> SliceResult:
        if source == "wencai":
            reason = degraded_reason or "PRIMARY_SOURCE_MISSING_OR_INCOMPLETE"
        elif source == "legacy_redis_q2":
            reason = degraded_reason or "PRIMARY_SOURCE_MISSING_OR_INCOMPLETE"
        else:
            reason = None
        missing_asof = any(row.get("pit_unknown") for row in rows)
        status = "DEGRADED" if degraded else ("PARTIAL" if future_filtered or missing_asof else "READY")
        if degraded and (future_filtered or missing_asof):
            status = "DEGRADED"
        quality = dict(payload.get("quality") or {})
        quality.update({"coverage": coverage, "partial": status != "READY"})
        if request.slice_id == "auction_snapshot":
            tag = str(request.params.get("tag", ""))
            for row in rows:
                row.setdefault("auction_tag", tag)
        if source == "wencai" and request.slice_id == "auction_snapshot":
            quality["semantics"] = "WENCAI_AUCTION_NOT_TD_FINAL"
            quality["input_universe"] = "WENCAI_QUERY_RESULT_NOT_FULL_MARKET"
            quality["finality"] = "NOT_EQUIVALENT_TO_TD_FINAL"
        if source == "legacy_redis_q2" and request.slice_id == "auction_snapshot":
            quality["field_gaps"] = payload.get("field_gaps", ["match_amount_yuan may be missing"])
        meta = {"slice_id": request.slice_id, "trade_date": request.trade_date,
                "asof": request.asof, "source": source, "status": status,
                "available_at": payload.get("available_at"), "quality": quality,
                "lineage": lineage, "legacy_dependency": source == "legacy_redis_q2",
                "degraded": degraded}
        if degraded:
            meta["degraded_reason"] = reason or degraded_reason or "FALLBACK_SOURCE_SELECTED"
        if future_filtered:
            meta["reason"] = "FUTURE_ROWS_FILTERED"
        elif missing_asof:
            meta["reason"] = "NO_ASOF"
        return SliceResult.create(rows, meta)

    def _derive_limit_inputs(self, request: SliceRequest) -> Mapping[str, Any] | None:
        if self.limit_inputs is None:
            return None
        payload = self.limit_inputs(request)
        if not isinstance(payload, Mapping):
            return None
        rows, _ = _visible(payload, request)
        derived = []
        for row in rows:
            symbol = str(row.get("symbol", "")).zfill(6)
            prices = limit_prices(row.get("pre_close"), symbol,
                                  unlimited=bool(row.get("unlimited") or row.get("is_new_listing")))
            item = dict(row, symbol=symbol, source="price_derived", sealed=None)
            item["limit_up_price"] = prices[0] if prices else None
            item["limit_down_price"] = prices[1] if prices else None
            current = _number(row.get("price", row.get("close")))
            high, low = _number(row.get("high")), _number(row.get("low"))
            up, down = prices or (None, None)
            unlimited = bool(row.get("unlimited") or row.get("is_new_listing"))
            item["at_limit_up_price"] = is_limit_up(current, row.get("pre_close"), symbol,
                                                    unlimited=unlimited) if current is not None else None
            item["at_limit_down_price"] = is_limit_down(current, row.get("pre_close"), symbol,
                                                        unlimited=unlimited) if current is not None else None
            item["touched_limit_up_price"] = touched_limit(high, row.get("pre_close"), symbol,
                                                           unlimited=unlimited) if high is not None else None
            item["touched_limit_down_price"] = touched_limit_down(low, row.get("pre_close"), symbol,
                                                                   unlimited=unlimited) if low is not None else None
            item["price_proxy"] = True
            if item["at_limit_up_price"] is True:
                item["limit_state_price_proxy"] = 1
            elif item["at_limit_down_price"] is True:
                item["limit_state_price_proxy"] = -1
            elif current is not None and prices is not None:
                item["limit_state_price_proxy"] = 0
            else:
                item["limit_state_price_proxy"] = None
            if request.slice_id == "limit_up_today":
                item["limit_up_today"] = item["at_limit_up_price"]
                item["today_limit_up_quality"] = "PRICE_PROXY_NOT_SEAL"
            derived.append(item)
        return {**dict(payload), "rows": derived}

    def _redis_auction(self, request: SliceRequest) -> Mapping[str, Any] | None:
        tag = str(request.params.get("tag", "0925"))
        field = {"0920": "a20", "0924": "a24", "0925": "a25"}.get(tag)
        if field is None:
            return None
        rows: dict[str, dict[str, Any]] = {}
        expected_count: int | None = None
        anchor_key = f"market:auction:anchor:{request.trade_date.replace('-', '')}"
        try:
            raw = self.redis.get(anchor_key)
            if raw:
                decoded = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
                if isinstance(decoded, Mapping):
                    for symbol, record in decoded.items():
                        if isinstance(record, Mapping):
                            item = {"symbol": str(symbol).zfill(6), "source": "legacy_redis_q2",
                                    "match_price": record.get("price"),
                                    "match_amount_yuan": record.get("amount"),
                                    "rest_bid_yuan": record.get("bid_amount"),
                                    "change_pct_ratio": record.get("change_pct"),
                                    "event_time": record.get("event_time"),
                                    "available_at": record.get("available_at") or self._read_time()}
                            rows[item["symbol"]] = item
        except (AttributeError, OSError, ValueError, TypeError):
            pass
        try:
            symbols = sorted({(v.decode() if isinstance(v, bytes) else str(v)).zfill(6)
                              for v in self.redis.smembers(f"q2:active:{request.trade_date}")})
            expected_count = len(symbols)
            pipe = self.redis.pipeline(transaction=False)
            for symbol in symbols:
                pipe.hgetall(f"q2:{symbol}")
            for symbol, raw in zip(symbols, pipe.execute() if symbols else []):
                fields = {(k.decode() if isinstance(k, bytes) else str(k)):
                          (v.decode() if isinstance(v, bytes) else v) for k, v in (raw or {}).items()}
                price = _number(fields.get(field))
                if price is None or price <= 0:
                    continue
                item = rows.setdefault(symbol, {"symbol": symbol, "source": "legacy_redis_q2"})
                item["match_price_milli"] = int(price)
                item["match_amount_yuan"] = None
                item["price_status"] = "INDICATIVE" if tag in {"0920", "0924"} else "FINALITY_UNKNOWN"
                item["event_time"] = self._q2_time(fields.get("ts"))
                item["available_at"] = self._read_time()
                item["precision"] = "q2_anchor_price_milli; no matched amount for 0920/0924"
        except (AttributeError, OSError, TypeError, ValueError):
            pass
        if not rows:
            return None
        return {"rows": list(rows.values()), "expected_count": expected_count or len(rows),
                "field_gaps": ["a20/a24/a25 provide price only; matched amount is not joined across timestamps"]}

    @staticmethod
    def _q2_time(value: Any) -> str | None:
        stamp = _number(value)
        if stamp is None:
            return None
        try:
            return datetime.fromtimestamp(stamp / 1000, SHANGHAI).isoformat()
        except (OverflowError, OSError, ValueError):
            return None

    def _redis_q2_limit(self, request: SliceRequest) -> Mapping[str, Any] | None:
        if self.redis is None:
            return None
        try:
            symbols = sorted({(v.decode() if isinstance(v, bytes) else str(v)).zfill(6)
                              for v in self.redis.smembers(f"q2:active:{request.trade_date}")})
            pipe = self.redis.pipeline(transaction=False)
            for symbol in symbols:
                pipe.hgetall(f"q2:{symbol}")
            rows = []
            for symbol, raw in zip(symbols, pipe.execute() if symbols else []):
                fields = {(k.decode() if isinstance(k, bytes) else str(k)):
                          (v.decode() if isinstance(v, bytes) else v) for k, v in (raw or {}).items()}
                if fields.get("ls") in (None, ""):
                    continue
                try:
                    state = int(fields["ls"])
                except (TypeError, ValueError):
                    state = None
                rows.append({"symbol": symbol, "limit_state": state,
                             "legacy_limit_state": state, "legacy_ln_large_net_yuan": _number(fields.get("ln")),
                             "event_time": self._q2_time(fields.get("ts")),
                             "available_at": self._read_time(),
                             "source": "legacy_redis_q2"})
            return {"rows": rows, "expected_count": len(symbols)} if rows else None
        except (AttributeError, OSError, TypeError, ValueError):
            return None

    def _wencai_auction(self, request: SliceRequest) -> Mapping[str, Any] | None:
        from ..wencai_fallback import fetch_auction_fallback

        result = fetch_auction_fallback(self.wencai_gate.fetch, date=request.trade_date,
                                        min_amount_yuan=0, max_queries=2, sleep_s=0)
        rows = []
        for row in result.rows:
            rows.append({"symbol": row["symbol"], "match_price": row.get("match_price"),
                         "match_amount_yuan": row.get("amount_yuan"),
                         "change_pct": row.get("match_pct"), "available_at": request.asof,
                         "source": "wencai", "price_status": "WENCAI_FINALITY_UNKNOWN"})
        return {"rows": rows, "expected_count": request.params.get("expected_count"),
                "available_at": self._read_time(), "query_log": result.query_log,
                "complete": result.complete, "labels": result.labels,
                "status": "PROVIDER_UNAVAILABLE" if self.wencai_gate.last_failure else "AVAILABLE",
                "reason": self.wencai_gate.last_failure,
                "quality": {"semantics": "WENCAI_AUCTION_NOT_TD_FINAL",
                            "input_universe": "WENCAI_QUERY_RESULT_NOT_FULL_MARKET"}}

    def _wencai_limit(self, request: SliceRequest) -> Mapping[str, Any] | None:
        from ..wencai_fallback import fetch_limit_up_fallback

        result = fetch_limit_up_fallback(self.wencai_gate.fetch, date=request.trade_date,
                                         max_queries=1, sleep_s=0)
        available_at = self._read_time()
        rows = [{"symbol": row["symbol"], "limit_state": 1,
                 "wencai_in_limit_up_pool": True,
                 "wencai_seal_evidence": row.get("sealed"),
                 "wencai_seal_amount_yuan": row.get("seal_amount_yuan"),
                 "available_at": available_at, "source": "wencai",
                 "state_evidence": "WENCAI_LIMIT_UP_POOL_MEMBERSHIP"}
                for row in result.rows]
        query_log = list(result.query_log)
        complete = result.complete and self.wencai_gate.last_failure is None
        labels = list(result.labels)
        # Limit-up slice is the explicit pool output. The limit_state slice also
        # needs down-pool membership, kept as auxiliary evidence without claiming
        # a seal amount or replacing exact price-derived state.
        if request.slice_id == "limit_state":
            date = request.trade_date
            prefix = f"{date[:4]}年{int(date[5:7])}月{int(date[8:10])}日"
            query = f"{prefix}跌停"
            try:
                raw = self.wencai_gate.fetch(query, True, 1000)
                if self.wencai_gate.last_failure:
                    complete = False
                    query_log.append({"query": query, "status": "provider_unavailable",
                                      "reason": self.wencai_gate.last_failure})
                    raw = None
                down_rows = self._wencai_raw_rows(raw)
                down_count = 0
                for raw_row in down_rows:
                    symbol = self._wencai_symbol(raw_row)
                    if symbol is None:
                        continue
                    rows.append({"symbol": symbol, "limit_state": -1,
                                 "wencai_in_limit_down_pool": True,
                                 "available_at": available_at, "source": "wencai",
                                 "state_evidence": "WENCAI_LIMIT_DOWN_POOL_MEMBERSHIP"})
                    down_count += 1
                if not self.wencai_gate.last_failure:
                    query_log.append({"query": query, "status": "ok",
                                      "row_count": len(down_rows), "normalized_count": down_count,
                                      "error_type": None})
            except Exception as exc:
                complete = False
                query_log.append({"query": query, "status": "failed",
                                  "error_type": type(exc).__name__})
            labels.append("WENCAI_LIMIT_DOWN_POOL_AUXILIARY")
        return {"rows": rows, "expected_count": request.params.get("expected_count"),
                "available_at": available_at, "query_log": query_log,
                "complete": complete, "labels": labels,
                "status": "PROVIDER_UNAVAILABLE" if self.wencai_gate.last_failure else "AVAILABLE",
                "reason": self.wencai_gate.last_failure,
                "quality": {"semantics": "WENCAI_POOL_EVIDENCE_NOT_SEAL_TRUTH",
                            "limit_down_is_auxiliary": request.slice_id == "limit_state"}}

    @staticmethod
    def _wencai_raw_rows(value: Any) -> list[dict[str, Any]]:
        if value is None or isinstance(value, Mapping):
            return [dict(value)] if value else []
        if hasattr(value, "to_dict"):
            try:
                records = value.to_dict(orient="records")
                if isinstance(records, list):
                    return [dict(row) for row in records if isinstance(row, Mapping)]
            except (TypeError, ValueError):
                return []
        try:
            return [dict(row) for row in value if isinstance(row, Mapping)]
        except TypeError:
            return []

    @staticmethod
    def _wencai_symbol(row: Mapping[str, Any]) -> str | None:
        for key in ("股票代码", "证券代码", "代码", "symbol"):
            if row.get(key) is not None:
                digits = "".join(char for char in str(row[key]) if char.isdigit())
                if digits:
                    return digits.zfill(6) if len(digits) <= 6 else digits[-6:]
        return None

    @staticmethod
    def _read_time() -> str:
        return datetime.now(SHANGHAI).isoformat()
