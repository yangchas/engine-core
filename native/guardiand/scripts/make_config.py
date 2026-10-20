#!/usr/bin/env python3
"""Fill handler argv/env into a migrated guardiand config. usage: make_config.py <migrated.json> <out.json> <repo> <python>"""
import json, sys

src, dst, repo, py = sys.argv[1:5]
cfg = json.load(open(src))
bridge = f"{repo}/examples/guardian_handler.py"
env = ["REDIS_URL", "CORE_REDIS_URL", "CORE_DATA_ROOT", "TD_CONFIG", "PYTHONHASHSEED", "PYTHONPATH",
       "GUARDIAN_DRY_RUN", "GUARDIAN_EMAIL_SELF", "HOME", "POSTHOC_DEV", "CORE_BAOSTOCK_PROBE_COMMAND"]
cfg["handlers"]["legacy_calendar_refresh"] = {"supports_degrade": True}
if not any(e["id"] == "calendar_refresh_0800" for e in cfg["events"]):
    cfg["events"].insert(0, {"id": "calendar_refresh_0800", "enabled": True, "calendar": "every_day",
        "trigger": {"type": "window", "start": "08:00:00", "deadline": "08:15:00"},
        "handler": "legacy_calendar_refresh", "timeout": "300s", "retry": {"max_attempts": 2},
        "on_deadline": "missed", "catchup": "run_in_window"})
for name, h in cfg["handlers"].items():
    h.update({"argv": [py, bridge, "run"], "gate_argv": [py, bridge, "gate"], "cwd": repo, "env_allow": env})
cfg["handlers"]["alert"] = {"argv": [py, bridge, "alert"], "cwd": repo, "env_allow": env}
late_by_handler = {
    "legacy_premarket_data": "15:00:00",
    "legacy_kaipan_fetch": "15:00:00",
    "legacy_calendar_refresh": "15:00:00",
    "legacy_postmarket_data": "23:30:00",
    "legacy_baostock_probe": "23:30:00",
    "legacy_night_report": "23:59:00",
    "legacy_auction_report": "11:30:00",
    "legacy_open_report": "11:30:00",
}
for event in cfg["events"]:
    event.pop("late_until", None)
    late_until = late_by_handler.get(event.get("handler"))
    if late_until:
        event["late_until"] = late_until
json.dump(cfg, open(dst, "w"), ensure_ascii=False, indent=1)
print("handlers:", len(cfg["handlers"]), "events:", len(cfg["events"]))
