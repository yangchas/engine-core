"""Small pykaipan compatibility layer for an explicitly sized full reason history request."""
from __future__ import annotations

import importlib
import re
import threading
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from typing import Any


_SDK_LOCK = threading.RLock()


def get_ban_reasons(symbol: str, *, st: int = 200) -> Any:
    """Call the installed SDK while overriding its hard-coded ``st=1`` to the requested page size.

    Older pykaipan releases expose only ``getBanReasons(code=...)``. Its request function is patched under a lock
    for this one call and restored in ``finally``; credentials stay inside the SDK and are never returned here.
    """
    size = int(st)
    if size < 1 or size > 200:
        raise ValueError("kaipan_reason_st_out_of_range")
    sdk = importlib.import_module("pykaipan.pykaipan")
    original = sdk.reques

    def sized_request(url: str, *args: Any, **kwargs: Any) -> Any:
        parts = urlsplit(url)
        query = parse_qsl(parts.query, keep_blank_values=True)
        query_map = dict(query)
        if query_map.get("a") != "GetDayZhangTing" or query_map.get("StockID") != str(symbol).zfill(6):
            return original(url, *args, **kwargs)
        replaced = False
        updated = []
        for key, value in query:
            if key.lower() == "st":
                updated.append((key, str(size)))
                replaced = True
            else:
                updated.append((key, value))
        if not replaced:
            updated.append(("st", str(size)))
        return original(urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(updated), parts.fragment)),
                        *args, **kwargs)

    with _SDK_LOCK:
        sdk.reques = sized_request
        try:
            return sdk.getBanReasons(code=str(symbol).zfill(6))
        finally:
            sdk.reques = original
