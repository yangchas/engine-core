#!/usr/bin/env python3
"""Compatibility CLI for the Core live report renderer."""
from engine_core.live_pages import *  # noqa: F401,F403
from engine_core import live_pages as _live_pages

# Preserve the historic import surface, including private helpers used by fixtures.
globals().update({name: value for name, value in vars(_live_pages).items()
                  if not name.startswith("__")})

if __name__ == "__main__":
    raise SystemExit(_live_pages.main())
