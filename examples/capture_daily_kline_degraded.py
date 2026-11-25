"""Read q2 final bars with optional WenCai field fallback after 15:05."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from engine_core.daily_kline_degraded import main


if __name__ == "__main__":
    raise SystemExit(main())
