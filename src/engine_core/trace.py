"""Trace sinks for local vertical-slice verification."""

from __future__ import annotations

import sys
from typing import TextIO

from .contracts import StrategyResult, canonical_json


class JsonTraceSink:
    """Write one deterministic JSON object per strategy result."""

    def __init__(self, stream: TextIO | None = None) -> None:
        # Resolve stdout at construction time so redirected/captured output is
        # respected; a default argument would retain the import-time stream.
        self._stream = sys.stdout if stream is None else stream

    def emit(self, result: StrategyResult) -> None:
        self._stream.write(canonical_json(result) + "\n")
        self._stream.flush()
