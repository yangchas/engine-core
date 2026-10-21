#!/usr/bin/env python3
"""Fill handler argv/env into a migrated guardiand config. usage: make_config.py <migrated.json> <out.json> <repo> <python>"""
import json, sys

src, dst, repo, py = sys.argv[1:5]
cfg = json.load(open(src))
cfg.setdefault("heavy_max_load1", 0)
bridge = f"{repo}/examples/guardian_handler.py"
env = ["REDIS_URL", "CORE_REDIS_URL", "CORE_DATA_ROOT", "TD_CONFIG", "PYTHONHASHSEED", "PYTHONPATH",
       "GUARDIAN_DRY_RUN", "GUARDIAN_EMAIL_SELF", "HOME", "POSTHOC_DEV", "CORE_BAOSTOCK_PROBE_COMMAND"]
cfg["handlers"]["legacy_calendar_refresh"] = {"supports_degrade": True}
for name in ("legacy_boot_trade_day", "legacy_boot_data_health", "legacy_boot_reconcile",
             "legacy_boot_apply_plan", "legacy_fetch_item"):
    cfg["handlers"].setdefault(name, {})
if not any(e["id"] == "calendar_refresh_0800" for e in cfg["events"]):
    cfg["events"].insert(0, {"id": "calendar_refresh_0800", "enabled": True, "calendar": "every_day",
        "trigger": {"type": "window", "start": "08:00:00", "deadline": "08:15:00"},
        "handler": "legacy_calendar_refresh", "timeout": "300s", "retry": {"max_attempts": 2},
        "on_deadline": "missed", "catchup": "run_in_window"})
for name, h in cfg["handlers"].items():
    h.update({"argv": [py, bridge, "run"], "gate_argv": [py, bridge, "gate"], "cwd": repo, "env_allow": env})
# idle load on this 2-CPU box is ~1.6; defer heavy jobs above 1.5x CPUs (default chosen 2026-10-20, tune with data)
cfg["heavy_max_load1"] = 3.0
cfg["handlers"]["alert"] = {"argv": [py, bridge, "alert"], "cwd": repo, "env_allow": env}
late_by_handler = {
    "legacy_premarket_data": "09:10:00",
    "legacy_kaipan_fetch": "15:00:00",
    "legacy_calendar_refresh": "15:00:00",
    "legacy_postmarket_data": "23:30:00",
    "legacy_baostock_probe": "23:30:00",
    "legacy_night_report": "23:59:00",
}
registry_path = f"{repo}/src/engine_core/guardian/datasets.registry.json"
try:
    registry_doc = json.load(open(registry_path, encoding="utf-8"))
    registry = registry_doc.get("items", [])
    cfg["heavy_cutoffs"] = registry_doc.get("heavy_cutoffs", {})
except (OSError, json.JSONDecodeError):
    registry = []
fetch_events = {item.get("guardian_event") for item in registry
                if item.get("guardian_event") and item.get("fetch_job")}
for event in cfg["events"]:
    event.pop("late_until", None)
    event_id = event.get("id", event.get("event_id", ""))
    handler = event.get("handler", "")
    canonical_handler = handler.removeprefix("legacy_")
    late_until = late_by_handler.get(handler)
    if late_until:
        event["late_until"] = late_until
    if handler in {"legacy_postmarket_data", "legacy_baostock_probe", "legacy_close_report", "legacy_night_report"}:
        event["cost"] = "heavy"
        event["on_phase_mismatch"] = "defer"
        event["phases"] = ["NIGHT", "PRE_MARKET", "MIDDAY_BREAK", "CLOSE", "POST_MARKET"]
    elif handler == "legacy_premarket_data":
        event["cost"] = "medium"
        event["on_phase_mismatch"] = "defer"
        event["phases"] = ["NIGHT", "PRE_MARKET"]
        event["late_until"] = "09:10:00"
    elif canonical_handler in {"kaipan_fetch", "calendar_refresh", "kaipan_hot_capture", "snapshot_check", "auction_watch"}:
        event["cost"] = "light"
    # time-point captures (kaipan_hot_*) are gap-only: a missed point is recorded as a gap, never re-captured late
    is_data_event = handler in late_by_handler or bool(late_until)
    if is_data_event:
        retry = event.setdefault("retry", {})
        retry["until_late"] = True
        retry["backoff"] = "60s"
        # baostock probe checkpoints fetch daily_kline, which the registry already covers (daily_kline.fetch_job)
        if event_id not in fetch_events and handler != "legacy_baostock_probe":
            event["on_start"] = "reconcile"
        else:
            event.pop("on_start", None)
    if canonical_handler in {"premarket_data", "kaipan_fetch", "postmarket_data", "baostock_probe", "calendar_refresh", "night_report"}:
        retry = event.setdefault("retry", {})
        retry["max_attempts"] = 3
        retry["backoff"] = "60s"
    if handler == "legacy_baostock_probe" and event_id.endswith(("1730", "1740", "1830")):
        event.setdefault("retry", {})["silent_until_last"] = True
boot_events = [
    {"id": "boot_trade_day", "enabled": True, "calendar": "every_day", "trigger": {"type": "startup", "order": 10}, "handler": "legacy_boot_trade_day", "timeout": "20s", "retry": {"max_attempts": 2}},
    {"id": "boot_data_health", "enabled": True, "calendar": "every_day", "trigger": {"type": "startup", "order": 20, "after": ["boot_trade_day"]}, "handler": "legacy_boot_data_health", "timeout": "60s", "retry": {"max_attempts": 2}, "phases": ["any"]},
    {"id": "boot_reconcile", "enabled": True, "calendar": "every_day", "trigger": {"type": "startup", "order": 30, "after": ["boot_data_health"]}, "handler": "legacy_boot_reconcile", "timeout": "60s", "retry": {"max_attempts": 2}},
    {"id": "boot_apply_plan", "enabled": True, "calendar": "every_day", "trigger": {"type": "startup", "order": 40, "after": ["boot_reconcile"]}, "handler": "legacy_boot_apply_plan", "timeout": "300s", "retry": {"max_attempts": 2}},
    {"id": "fetch_item", "enabled": True, "calendar": "every_day", "trigger": {"type": "startup", "order": 50, "after": ["boot_apply_plan"]}, "handler": "legacy_fetch_item", "timeout": "300s", "cost": "heavy", "phases": ["NIGHT", "PRE_MARKET", "MIDDAY_BREAK", "CLOSE", "POST_MARKET"], "retry": {"max_attempts": 3, "backoff": "60s"}},
]
existing = {e.get("id") for e in cfg["events"]}
boot_ids = {x["id"] for x in boot_events}
cfg["events"] = boot_events + [e for e in cfg["events"] if e.get("id") not in boot_ids]
json.dump(cfg, open(dst, "w"), ensure_ascii=False, indent=1)
print("handlers:", len(cfg["handlers"]), "events:", len(cfg["events"]))
