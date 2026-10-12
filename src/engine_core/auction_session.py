"""Source-independent auction-anchor session orchestration.

The session accepts already-observed canonical Q2 rows. It owns the shared
auction revision ledger and deterministic timer-signal identities, but has no
Redis, TDengine, RabbitMQ, replay-file, or Engine-client dependency. Adapters
and runners remain responsible for reading data and submitting returned
signals to an Engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from .auction_timeline import (
    AuctionAnchorRevisionV3,
    AuctionTimeline,
    AuctionTimingPolicyV1,
)
from .contracts import EngineSignal, SignalKind


AUCTION_TAGS = ("0920", "0924", "0925")


@dataclass(frozen=True)
class AuctionSessionObservationV1:
    """Outcome of one source observation attempt."""

    status: str
    revision: Optional[AuctionAnchorRevisionV3] = None
    timer_signal: Optional[EngineSignal] = None


class AuctionAnchorSessionV1:
    """Shared auction-anchor revision and timer logic for any Q2 source.

    ``rows`` must already be a symbol-keyed mapping of canonical Q2 row
    mappings. Source adapters may provide Q2Frame-accumulated state or a
    read-only Redis Q2 projection; this class does not distinguish or fetch
    either source. Source-specific evidence is passed explicitly through
    ``observed_at_ms`` and ``source_layers``.
    """

    def __init__(
        self,
        trade_date: str,
        expected_symbols: Sequence[Any],
        *,
        policies: Optional[Mapping[str, AuctionTimingPolicyV1]] = None,
        signal_prefix: str = "q2frame",
    ) -> None:
        if not isinstance(signal_prefix, str) or not signal_prefix.strip():
            raise ValueError("signal_prefix must be a non-empty string")
        self.trade_date = trade_date
        self.expected_symbols = tuple(expected_symbols)
        self.policies = dict(
            policies
            or {tag: AuctionTimingPolicyV1.default(tag) for tag in AUCTION_TAGS}
        )
        self.timeline = AuctionTimeline(trade_date, self.policies)
        self.signal_prefix = signal_prefix.strip()

    def first_observable_ms(self, tag: str) -> int:
        try:
            policy = self.policies[tag]
        except KeyError as exc:
            raise ValueError("unsupported auction tag") from exc
        return policy.at(self.trade_date)["first_observable_ms"]

    def observe(
        self,
        tag: str,
        rows: Mapping[str, Mapping[str, Any]],
        *,
        evaluation_time_ms: int,
        observed_at_ms: Optional[int] = None,
        source_layers: Sequence[str] = (),
    ) -> AuctionAnchorRevisionV3:
        """Create or reuse a content revision from already-read source rows."""

        return self.timeline.observe(
            tag,
            rows,
            evaluation_time_ms=evaluation_time_ms,
            expected_symbols=self.expected_symbols,
            observed_at_ms=observed_at_ms,
            source_layers=source_layers,
        )

    def barrier_timer_signal(
        self, tag: str, logical_time_ms: int, barrier_index: int
    ) -> EngineSignal:
        """Build the stable first-observation TIMER for a scheduled barrier."""

        self._validate_tag(tag)
        if type(barrier_index) is not int or barrier_index < 0:
            raise ValueError("barrier_index must be a non-negative integer")
        return self._timer_signal(
            signal_id=f"{self.signal_prefix}-timer:{tag}",
            logical_time_ms=logical_time_ms,
            signal_seq=1_000_000_000 + barrier_index,
            tag=tag,
        )

    def observe_late_revision(
        self,
        tag: str,
        rows: Mapping[str, Mapping[str, Any]],
        *,
        logical_time_ms: int,
        observed_at_ms: Optional[int] = None,
        source_layers: Sequence[str] = (),
    ) -> AuctionSessionObservationV1:
        """Observe changed content after an anchor and emit only a new revision timer.

        A prior anchor observation is required, and the policy's first
        observable time is a soft scheduling boundary. Equal-content reads
        refresh evidence timing through ``AuctionTimeline`` but do not create
        another revision or TIMER.
        """

        tag_index = self._validate_tag(tag)
        if logical_time_ms < self.first_observable_ms(tag):
            return AuctionSessionObservationV1("BEFORE_FIRST_OBSERVABLE")
        previous = self.timeline.latest(tag)
        if previous is None:
            return AuctionSessionObservationV1("NO_PRIOR_ANCHOR_OBSERVATION")
        revision = self.observe(
            tag,
            rows,
            evaluation_time_ms=logical_time_ms,
            observed_at_ms=observed_at_ms,
            source_layers=source_layers,
        )
        if revision.revision <= previous.revision:
            return AuctionSessionObservationV1("UNCHANGED", revision)
        signal = self._timer_signal(
            signal_id=(
                f"{self.signal_prefix}-revision-timer:{tag}:{revision.revision}"
            ),
            logical_time_ms=logical_time_ms,
            signal_seq=2_000_000_000 + tag_index * 1_000_000 + revision.revision,
            tag=tag,
        )
        return AuctionSessionObservationV1("REVISION_EMITTED", revision, signal)

    def observe_late_start(
        self,
        tag: str,
        rows: Mapping[str, Mapping[str, Any]],
        *,
        logical_time_ms: int,
        anchor_field_observed: bool,
        observed_at_ms: Optional[int] = None,
        source_layers: Sequence[str] = (),
    ) -> AuctionSessionObservationV1:
        """Observe one late-start cohort without backdating its TIMER.

        ``anchor_field_observed`` is adapter evidence about raw field presence;
        it is deliberately separate from a normalized zero/missing value.
        """

        tag_index = self._validate_tag(tag)
        if not anchor_field_observed:
            return AuctionSessionObservationV1("ANCHOR_FIELD_NOT_PRESENT")
        if logical_time_ms < self.first_observable_ms(tag):
            return AuctionSessionObservationV1("BEFORE_FIRST_OBSERVABLE")
        revision = self.observe(
            tag,
            rows,
            evaluation_time_ms=logical_time_ms,
            observed_at_ms=observed_at_ms,
            source_layers=source_layers,
        )
        signal = self._timer_signal(
            signal_id=(
                f"{self.signal_prefix}-late-start-timer:{tag}:{revision.revision}"
            ),
            logical_time_ms=logical_time_ms,
            signal_seq=2_000_000_000 + tag_index * 1_000_000 + revision.revision,
            tag=tag,
        )
        return AuctionSessionObservationV1("LATE_START_OBSERVED", revision, signal)

    def _validate_tag(self, tag: str) -> int:
        try:
            return AUCTION_TAGS.index(tag)
        except ValueError as exc:
            raise ValueError("unsupported auction tag") from exc

    @staticmethod
    def _timer_signal(
        *, signal_id: str, logical_time_ms: int, signal_seq: int, tag: str
    ) -> EngineSignal:
        return EngineSignal(
            signal_id=signal_id,
            logical_time_ms=logical_time_ms,
            signal_seq=signal_seq,
            signal_kind=SignalKind.TIMER,
            payload={"trigger_id": f"AUCTION_{tag}"},
        )
