"""Persistent, content-addressed storage for pure derived nodes.

Node lookup intentionally ignores ``available_at``: a derived result is a pure
function of its named input versions and code version, so an identical
fingerprint is the same result rather than newly observed market information.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from .storage import ArtifactCatalog, canonical_json


def node_fingerprint(node_name: str, code_version: str, inputs: Mapping[str, str]) -> str:
    if not node_name or not code_version:
        raise ValueError("node_name and code_version are required")
    return hashlib.sha256(canonical_json({
        "node": node_name, "code_version": code_version, "inputs": dict(inputs),
    })).hexdigest()


class NodeStore:
    def __init__(self, catalog: ArtifactCatalog, clock: Any = None):
        self.catalog = catalog
        self.clock = clock

    def get(self, node_name: str, trade_date: str, fingerprint: str) -> list[dict[str, Any]] | Any | None:
        versions = (self.catalog.read().get("datasets", {}).get(f"node__{node_name}", {})
                    .get("dates", {}).get(trade_date, []))
        for item in reversed(versions):
            if item.get("node_fingerprint") == fingerprint:
                rows = self.catalog.read_rows(Path(item["manifest"]))
                if len(rows) == 1 and set(rows[0]) == {"__node_value__"}:
                    return rows[0]["__node_value__"]
                return rows
        return None

    def put(self, node_name: str, trade_date: str, fingerprint: str,
            rows: list[dict[str, Any]], *, inputs: Mapping[str, str],
            code_version: str, asof: str | None = None) -> dict[str, Any]:
        if not node_name or not code_version:
            raise ValueError("node_name and code_version are required")
        if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
            raise TypeError("rows must be a list of dictionaries")
        existing = self._manifest_for(node_name, trade_date, fingerprint)
        if existing is not None:
            return existing
        manifest = {"node_name": node_name, "code_version": code_version,
                    "inputs": dict(inputs), "node_fingerprint": fingerprint,
                    "asof": asof, "source_asof": asof or "UNKNOWN"}
        return self.catalog.commit(f"node__{node_name}", trade_date, rows, manifest,
                                   source="node", authority="LIVE", clock=self.clock)

    def compute(self, node_name: str, trade_date: str, code_version: str,
                inputs: Mapping[str, str], fn: Callable[[], Any], *,
                asof: str | None = None) -> tuple[Any, bool]:
        fingerprint = node_fingerprint(node_name, code_version, inputs)
        found = self.get(node_name, trade_date, fingerprint)
        if found is not None:
            return found, True
        value = fn()
        if isinstance(value, list) and all(isinstance(row, dict) for row in value):
            rows = value
            returned = value
        else:
            canonical_json(value)  # reject values that cannot be persisted
            rows = [{"__node_value__": value}]
            returned = value
        self.put(node_name, trade_date, fingerprint, rows, inputs=inputs,
                 code_version=code_version, asof=asof)
        return returned, False

    def _manifest_for(self, node_name: str, trade_date: str, fingerprint: str) -> dict[str, Any] | None:
        versions = (self.catalog.read().get("datasets", {}).get(f"node__{node_name}", {})
                    .get("dates", {}).get(trade_date, []))
        for item in reversed(versions):
            if item.get("node_fingerprint") == fingerprint:
                return json.loads(Path(item["manifest"]).read_text(encoding="utf-8"))
        return None
