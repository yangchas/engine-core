"""Runs ``python <script> | -c <code> | -m <module> ...`` with the replay stand-ins installed first (see replay_python)."""
import os
import runpy
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import replay_handler  # noqa: E402

replay_handler.install()
args = sys.argv[1:]
if not args:
    raise SystemExit("replay_python: interactive use is not supported")
if args[0] == "-c":
    sys.argv = ["-c", *args[2:]]
    exec(compile(args[1], "<string>", "exec"), {"__name__": "__main__"})
elif args[0] == "-m":
    sys.argv = [args[1], *args[2:]]
    runpy.run_module(args[1], run_name="__main__", alter_sys=True)
else:
    sys.argv = args
    runpy.run_path(args[0], run_name="__main__")
