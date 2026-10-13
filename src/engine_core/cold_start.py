"""What a session that starts at an arbitrary time can and cannot rebuild.

Pure and clock-free: given a ``SessionPlan`` and an explicit start time it
classifies the start and states, per information item, whether it is still
observable, only reconstructable from a history source, or lost.  Nothing is
backdated: a late-observed anchor keeps the start time as its observation
time.  Evidence for the rules is recorded in ``EVIDENCE``.

Rules (Q2 holds current state only):

* ``a20``/``a24``/``a25`` persist in Q2 for the day once produced, so a late
  start can read them, with the start time as observation time.
* A late Shenzhen final (``ph==0``, ``vol>0``) is recoverable while its row
  is still visible.  Continuous trading overwrites the row from 09:30:00, so
  from then on only symbols with no later tick (e.g. halted at the open) can
  be recovered; ``WINDOW_CLOSED`` means "expect none", not a hard gate.
* 0920/0924 amounts are overwritten by final amounts; the 0924->0925 amount
  delta is reconstructable only from TD ``auction_snapshot_v2`` rows.
* The 09:32 opening cutoff cannot be rebuilt from current quotes; it needs the
  TD event-time replay.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Tuple

from .auction_timeline import AuctionTimingPolicyV1
from .clock import local_datetime_ms
from .contracts import semantic_hash
from .session import SessionPlan
from .timers import TimerSpec, due_timer_firings

COLD_START_CONTRACT_VERSION = "ColdStartAssessmentV1"

PREMARKET = "PREMARKET"
AUCTION_COLLECTING = "AUCTION_COLLECTING"
AUCTION_FINAL = "AUCTION_FINAL"
OPENING_FORMING = "OPENING_FORMING"
MORNING_SESSION = "MORNING_SESSION"
LUNCH_BREAK = "LUNCH_BREAK"
AFTERNOON_SESSION = "AFTERNOON_SESSION"
POSTMARKET = "POSTMARKET"

PENDING = "PENDING"
OBSERVABLE_AT_START = "OBSERVABLE_AT_START"
NOT_YET_APPLICABLE = "NOT_YET_APPLICABLE"
WINDOW_OPEN = "WINDOW_OPEN"
WINDOW_CLOSED = "WINDOW_CLOSED"
AVAILABLE_FROM_SESSION = "AVAILABLE_FROM_SESSION"
TD_RECONSTRUCTION_REQUIRED = "TD_RECONSTRUCTION_REQUIRED"

EVIDENCE = (
    "2026-10-13 sealed capture 11:48: a20 3318 / a24 4315 / a25 4853 present, "
    "ph==0 recovery 0",
    "2026-10-13 live: late finals visible 09:26:04-09:27:05 (30/179/357 symbols), "
    "recovery 0 at 09:30:17 and 11:48",
    "2026-10-09 offline audit: a20/a24/a25 stable 09:25:06-09:42",
)

DEFAULT_TIMERS = (
    TimerSpec("AUCTION_0926", "09:26:00"),
    TimerSpec("OPENING_0932", "09:32:00"),
)


@dataclass(frozen=True)
class ColdStartAssessment:
    trade_date: str
    start_ms: int
    start_class: str
    status: str  # READY when nothing is lost by starting now, else PARTIAL
    anchors: Mapping[str, str]
    late_final_recovery: str
    theme_amount_delta_0924_0925: str
    opening_0932: str
    recovery_due_timers: Tuple[str, ...]
    unrecoverable: Tuple[str, ...]
    evidence: Tuple[str, ...] = EVIDENCE
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "content_hash", semantic_hash(self.as_mapping(hash_free=True)))

    def as_mapping(self, hash_free: bool = False) -> Mapping[str, object]:
        data = {
            "contract": COLD_START_CONTRACT_VERSION,
            "trade_date": self.trade_date,
            "start_ms": self.start_ms,
            "start_class": self.start_class,
            "status": self.status,
            "anchors": dict(self.anchors),
            "late_final_recovery": self.late_final_recovery,
            "theme_amount_delta_0924_0925": self.theme_amount_delta_0924_0925,
            "opening_0932": self.opening_0932,
            "recovery_due_timers": self.recovery_due_timers,
            "unrecoverable": self.unrecoverable,
            "evidence": self.evidence,
        }
        if not hash_free:
            data["content_hash"] = self.content_hash
        return data


def classify_start(plan: SessionPlan, start_ms: int) -> str:
    phase = plan.phase_at_ms(start_ms)
    at = lambda hms: local_datetime_ms(plan.trade_date, hms, timezone_name=plan.timezone_name)  # noqa: E731
    if phase == "PREMARKET":
        return PREMARKET
    if phase == "AUCTION":
        return AUCTION_COLLECTING if start_ms < at("09:25:00") else AUCTION_FINAL
    if phase == "INTRADAY":
        if start_ms < at("09:32:00"):
            return OPENING_FORMING
        return MORNING_SESSION if start_ms < at("11:30:00") else AFTERNOON_SESSION
    if phase == "LUNCH_BREAK":
        return LUNCH_BREAK
    if phase == "POSTMARKET":
        return POSTMARKET
    raise ValueError("unsupported session phase: %s" % phase)


def assess_cold_start(
    plan: SessionPlan,
    start_ms: int,
    *,
    timers: Iterable[TimerSpec] = DEFAULT_TIMERS,
) -> ColdStartAssessment:
    start_class = classify_start(plan, start_ms)
    trade_date = plan.trade_date
    at = lambda hms: local_datetime_ms(trade_date, hms, timezone_name=plan.timezone_name)  # noqa: E731

    anchors = {}
    for tag in ("0920", "0924", "0925"):
        first = AuctionTimingPolicyV1.default(tag).at(trade_date)["first_observable_ms"]
        anchors[tag] = PENDING if start_ms < first else OBSERVABLE_AT_START

    if start_ms < at("09:25:00"):
        recovery = NOT_YET_APPLICABLE
    elif start_ms < at("09:30:00"):
        recovery = WINDOW_OPEN
    else:
        recovery = WINDOW_CLOSED

    first_0924 = AuctionTimingPolicyV1.default("0924").at(trade_date)["first_observable_ms"]
    amount_delta = AVAILABLE_FROM_SESSION if start_ms < first_0924 else TD_RECONSTRUCTION_REQUIRED
    opening = PENDING if start_ms < at("09:32:00") else TD_RECONSTRUCTION_REQUIRED

    firings = due_timer_firings(
        plan, tuple(timers), previous_time_ms=None, current_time_ms=start_ms,
        origin="RECOVERY_CATCHUP",
    )
    lost = []
    if recovery == WINDOW_CLOSED:
        lost.append("late_final_recovery: symbols without a25 stay MISSING unless their ph==0 row is still unchanged (continuous trading overwrites it)")
    if amount_delta == TD_RECONSTRUCTION_REQUIRED:
        lost.append("theme_amount_delta_0924_0925: Q2 amounts overwritten; needs TD auction_snapshot_v2")
    if opening == TD_RECONSTRUCTION_REQUIRED:
        lost.append("opening_0932: current quotes are not the 09:32 cutoff; needs TD event-time replay")
    late_anchors = [tag for tag, state in anchors.items() if state == OBSERVABLE_AT_START and start_ms >= at("09:25:20")]
    if late_anchors:
        lost.append("anchor observation time is the start time, not the barrier time")
    return ColdStartAssessment(
        trade_date=trade_date,
        start_ms=start_ms,
        start_class=start_class,
        status="READY" if not lost else "PARTIAL",
        anchors=anchors,
        late_final_recovery=recovery,
        theme_amount_delta_0924_0925=amount_delta,
        opening_0932=opening,
        recovery_due_timers=tuple(item.timer_id for item in firings),
        unrecoverable=tuple(lost),
    )


__all__ = [
    "AUCTION_COLLECTING", "AUCTION_FINAL", "AFTERNOON_SESSION", "COLD_START_CONTRACT_VERSION",
    "ColdStartAssessment", "LUNCH_BREAK", "MORNING_SESSION", "OPENING_FORMING", "POSTMARKET",
    "PREMARKET", "assess_cold_start", "classify_start",
]
