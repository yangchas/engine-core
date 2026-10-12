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
from .clock import local_datetime_ms
from .contracts import EngineSignal, SignalKind


AUCTION_TAGS = ("0920", "0924", "0925")


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


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
        recover_final_from_phase_zero: bool = False,
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
        # Opt-in: the Shenzhen final-match snapshot (ph==0, vol>0) can arrive
        # long after 09:25:00 (observed 09:26:00-09:26:57), outside the
        # producer's a25 window.  When enabled, such a snapshot is remembered
        # per symbol and used as the 0925 anchor only where a25 is missing.
        self.recover_final_from_phase_zero = bool(recover_final_from_phase_zero)
        self._phase_zero_final: dict = {}
        self._final_lower_ms = local_datetime_ms(trade_date, "09:25:00")
        self._final_upper_ms = local_datetime_ms(trade_date, "09:30:00")

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

        if tag == "0925" and self.recover_final_from_phase_zero:
            rows = self._rows_with_phase_zero_final(rows)
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

    @property
    def phase_zero_recoveries(self) -> Mapping[str, Mapping[str, Any]]:
        """Symbols whose 0925 anchor came from a late ph==0 snapshot."""

        return {
            symbol: dict(value)
            for symbol, value in self._phase_zero_final.items()
            if value.get("used")
        }

    def _rows_with_phase_zero_final(
        self, rows: Mapping[str, Mapping[str, Any]]
    ) -> Mapping[str, Mapping[str, Any]]:
        enriched: dict = {}
        for symbol, row in rows.items():
            if not isinstance(row, Mapping):
                enriched[symbol] = row
                continue
            price = row.get("price_milli")
            volume = row.get("volume_lots")
            when = row.get("source_record_time_ms", row.get("source_time_ms"))
            if (
                row.get("phase") == 0
                and _positive_int(price)
                and _positive_int(volume)
                and isinstance(when, int)
                and not isinstance(when, bool)
                and self._final_lower_ms <= when < self._final_upper_ms
                and symbol not in self._phase_zero_final
            ):
                self._phase_zero_final[symbol] = {
                    "price_milli": int(price),
                    "volume_lots": int(volume),
                    "amount_yuan": row.get("amount_yuan"),
                    "source_time_ms": when,
                    "used": False,
                }
            recorded = self._phase_zero_final.get(symbol)
            anchor = row.get("auction_anchor_0925_price_milli")
            if recorded is not None and not _positive_int(anchor):
                patched = dict(row)
                patched["auction_anchor_0925_price_milli"] = recorded["price_milli"]
                quality = dict(row.get("auction_anchor_field_quality") or {})
                quality["a25"] = "PRESENT_VALUE"
                patched["auction_anchor_field_quality"] = quality
                recorded["used"] = True
                enriched[symbol] = patched
            else:
                enriched[symbol] = row
        return enriched

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
