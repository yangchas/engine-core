#!/usr/bin/env python3
"""Run one stage of the automated Core daily report pipeline."""
import sys

from engine_core.daily_pipeline import DailySources, main as _main


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    sources = None
    if "--allow-wencai-fallback" in args:
        from examples.run_wencai_fallback import _default_fetcher
        import os
        sources = DailySources(wencai_fetch=_default_fetcher(
            os.getenv("WENCAI_LEGACY_ROOT", "/home/exedev/repos/stock-situation-runtime")))
    return _main(args, sources=sources)


if __name__ == "__main__":
    raise SystemExit(main())
