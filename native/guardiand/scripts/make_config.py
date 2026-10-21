#!/usr/bin/env python3
"""Inject deployment-specific handler commands into a migrated guardian config."""
import json
import sys


src, dst, repo, py = sys.argv[1:5]
with open(src, encoding="utf-8") as stream:
    cfg = json.load(stream)
bridge = f"{repo}/examples/guardian_handler.py"
env = ["REDIS_URL", "CORE_REDIS_URL", "CORE_DATA_ROOT", "TD_CONFIG", "PYTHONHASHSEED", "PYTHONPATH",
       "GUARDIAN_DRY_RUN", "GUARDIAN_EMAIL_SELF", "HOME", "POSTHOC_DEV", "CORE_BAOSTOCK_PROBE_COMMAND"]
handlers = cfg.setdefault("handlers", {})
for event in cfg.get("events", []):
    name = event.get("handler")
    if name:
        handlers.setdefault(name, {})
for name, handler in handlers.items():
    name_for_bridge = name.removeprefix("legacy_")
    handler.update({"argv": [py, bridge, "run", "--handler", name_for_bridge],
                    "gate_argv": [py, bridge, "gate"], "cwd": repo, "env_allow": env})
handlers["alert"] = {"argv": [py, bridge, "alert", "--dry-run"], "cwd": repo, "env_allow": env}
with open(dst, "w", encoding="utf-8") as stream:
    json.dump(cfg, stream, ensure_ascii=False, indent=1)
    stream.write("\n")
print("handlers:", len(handlers), "events:", len(cfg.get("events", [])))
