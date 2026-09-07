from dataclasses import dataclass
from enum import StrEnum

from anomaly_detection_engine.models.market import MarketIdentity


class SignalType(StrEnum):
    """Discriminates the two persisted signal kinds (see
    storage.signal_repository.SignalRepository). Lives in models/, not
    analysis/ or storage/ -- both depend on it: analysis produces
    candidates of these types, storage persists/reconciles them by it,
    and neither is the right place for the other to depend on.
    """

    SUREBET = "SUREBET"
    VALUE_GAP = "VALUE_GAP"


# Bare aliases for the common case of passing/comparing a signal type
# without spelling out SignalType.SUREBET/SignalType.VALUE_GAP -- a
# StrEnum member compares equal to (and hashes the same as) its own
# string value, so these are interchangeable with the enum member itself
# and with every existing "SUREBET"/"VALUE_GAP" string already stored in
# the database.
SUREBET = SignalType.SUREBET
VALUE_GAP = SignalType.VALUE_GAP


class SignalStatus(StrEnum):
    """The lifecycle states a persisted signal moves through (see
    storage.signal_repository.SignalRepository): ACTIVE while a sweep
    keeps reconfirming the condition, RESOLVED when a sweep positively
    evaluates it and finds the condition gone (reconcile()), EXPIRED
    when detection simply stops being able to evaluate it at all and its
    event's lifecycle has run out (expire_active_signals). RESOLVED and
    EXPIRED are both terminal but mean different things -- see
    SignalRepository's own docstring.
    """

    ACTIVE = "ACTIVE"
    RESOLVED = "RESOLVED"
    EXPIRED = "EXPIRED"


# Same bare-alias reasoning as SUREBET/VALUE_GAP above.
ACTIVE = SignalStatus.ACTIVE
RESOLVED = SignalStatus.RESOLVED
EXPIRED = SignalStatus.EXPIRED


@dataclass(frozen=True)
class SignalIdentity:
    """The (event, market, outcome) triple a persisted signal is keyed
    on (see SignalRepository.reconcile()), and the same granularity a
    detection sweep reports as "actually evaluated" (see
    analysis.opportunity_detection.SurebetDetectionSweep/
    ValueGapDetectionSweep). outcome is None for SUREBET -- its identity
    has no per-outcome granularity (one arbitrage covers all three
    outcomes together).
    """

    event_id: str
    market: MarketIdentity
    outcome: str | None
