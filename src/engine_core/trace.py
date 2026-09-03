"""Trace sinks for local vertical-slice verification."""

from __future__ import annotations

import sys
from typing import TextIO

from .contracts import StrategyResult, canonical_json


class JsonTraceSink:
    """Write one deterministic JSON object per strategy result."""

    def __init__(self, stream: TextIO = sys.stdout) -> None:
        self._stream = stream

    def emit(self, result: StrategyResult) -> None:
        self._stream.write(canonical_json(result) + "\n")
        self._stream.flush()
