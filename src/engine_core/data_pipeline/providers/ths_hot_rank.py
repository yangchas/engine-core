"""Core-owned client for the public THS hot-rank endpoint."""
from __future__ import annotations

import time
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Mapping


URL = "https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/128.0 Safari/537.36",
           "Referer": "https://www.10jqka.com.cn/"}


class ProviderCircuitOpen(RuntimeError):
    pass


class _UrllibSession:
    def get(self, url: str, *, params: Mapping[str, str], headers: Mapping[str, str], timeout: float):
        request_url = url + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(request_url, headers=dict(headers), method="GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return _UrllibResponse(response.status, response.read())
        except urllib.error.HTTPError as exc:
            return _UrllibResponse(exc.code, exc.read())


class _UrllibResponse:
    def __init__(self, status_code: int, body: bytes):
        self.status_code, self.body = status_code, body

    def json(self):
        return json.loads(self.body.decode("utf-8"))


class ThsHotRankProvider:
    def __init__(self, *, session: Any = None, timeout: float = 10.0, retries: int = 3,
                 sleep_fn: Callable[[float], None] = time.sleep):
        if session is None:
            session = _UrllibSession()
        self.session = session
        self.timeout = min(10.0, max(0.1, float(timeout)))
        self.retries = max(1, int(retries))
        self.sleep_fn = sleep_fn
        self._tripped = False

    @staticmethod
    def normalize(payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, Mapping) or payload.get("status_code") != 0:
            raise ValueError("ths_hot_rank_invalid_payload")
        data = payload.get("data")
        source_rows = data.get("stock_list") if isinstance(data, Mapping) else None
        if not isinstance(source_rows, list):
            raise ValueError("ths_hot_rank_invalid_stock_list")
        result = []
        for item in source_rows:
            if not isinstance(item, Mapping):
                continue
            code = str(item.get("code") or "")
            symbol = code[-6:]
            if len(symbol) != 6 or not symbol.isdigit():
                continue
            row = {"symbol": symbol, "code": code, "name": str(item.get("name") or ""),
                   "rank": len(result) + 1, "source": "ths_hot_rank"}
            rate = item.get("rate")
            if rate is not None:
                try:
                    row["heat"] = float(rate)
                except (TypeError, ValueError):
                    pass
            result.append(row)
        return result

    def fetch(self, top_n: int = 100) -> list[dict[str, Any]]:
        if self._tripped:
            raise ProviderCircuitOpen("ths_hot_rank_circuit_open")
        for attempt in range(self.retries):
            try:
                response = self.session.get(URL, params={"stock_type": "a", "type": "day",
                    "list_type": "tech"}, headers=HEADERS, timeout=self.timeout)
                if response.status_code == 403:
                    self._tripped = True
                    raise ProviderCircuitOpen("ths_hot_rank_forbidden")
                if response.status_code != 200:
                    raise RuntimeError(f"ths_hot_rank_http_{response.status_code}")
                rows = self.normalize(response.json())
                if not rows:
                    raise RuntimeError("ths_hot_rank_empty_response")
                return rows[:max(0, int(top_n))]
            except ProviderCircuitOpen:
                raise
            except Exception as exc:
                if attempt + 1 == self.retries:
                    raise RuntimeError(f"ths_hot_rank_fetch_failed:{type(exc).__name__}") from None
                self.sleep_fn(max(10.0, min(60.0, 10.0 * (2 ** attempt))))
        raise RuntimeError("ths_hot_rank_fetch_failed")
