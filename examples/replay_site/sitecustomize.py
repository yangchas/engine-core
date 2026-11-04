"""Puts the replay stand-ins into EVERY Python process of a replayed day (C665).

examples/replay_handler.py installs them in the handler process, but the handlers start the real job scripts
(prep_trade_day.py, run_data_jobs.py ...) as child processes; without this file those children would talk to the real
Kaipan/Baostock/Redis/TD. A directory holding this file is put first on PYTHONPATH by examples/replay_day.py, so the
interpreter imports it at start-up. It does nothing unless CORE_REPLAY_DIR is set.
"""
import os
import sys

if os.environ.get("CORE_REPLAY_DIR"):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import replay_handler  # noqa: E402

    replay_handler.install()
