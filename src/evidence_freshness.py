"""How much of a decision's evidence was read on THIS tick.

A STANDALONE PART. It imports nothing from `src.evidence_gate`: the status
vocabulary — which status word means fresh, carried or absent, and which
carried words are known to be out of date — is handed to
`build_freshness_reader` BY VALUE, so the reader can be constructed and
tested against plain dictionaries with no gate, no pipeline and no config
anywhere in sight.

Disclosure only. Nothing here refuses, scores or holds a threshold.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace

logger = logging.getLogger(__name__)


#: THE THREE STATES A SEAT READ CAN BE IN, AND WHY THEY STAY APART.
#:
#: Owner ruling 2026-10-01 makes the desk re-test every holding against the
#: fresh-entry bar several times a day and SELL what fails it. That makes
#: "was this seat read in THIS run?" a question a sell can rest on, and
#: before this it could only be inferred. `carried_forward` is not fresh and
#: `absent` is not stale; conflating either way is the defect.
#:
#: There is NO threshold here and none may be added. `age_seconds` is
#: reported; no number says when an age becomes too old. That number is the
#: owner's and nothing in this module gates on it.
READ_REFRESHED = "refreshed_this_session"
READ_CARRIED = "carried_forward"
READ_ABSENT = "absent"
READ_UNKNOWN = "unknown"


@dataclass(frozen=True)
class EvidenceFreshness:
    """How much of this decision's evidence was read on THIS tick.

    Disclosure only. It carries no verdict, refuses nothing, and holds no
    threshold — see `STATUS_FRESHNESS`.
    """

    fresh: list[str] = field(default_factory=list)
    carried: list[str] = field(default_factory=list)
    absent: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    #: The subset of `carried` the desk KNOWS is superseded (`expired`).
    known_out_of_date: list[str] = field(default_factory=list)
    data_status: dict[str, str] = field(default_factory=dict)
    #: WHICH RUN produced this classification, and WHEN. Without these a
    #: later reader can only infer "was this seat refreshed in the session
    #: I am looking at?" from the shape of the surrounding row, and an
    #: inference is exactly what the sell-discipline consumer must not
    #: stand on. None when the caller did not stamp.
    run_id: str | None = None
    mode: str | None = None
    stamped_at: str | None = None
    #: {seat: {"run_id", "mode", "at"}} — the most recent EARLIER run in
    #: which this seat was read fresh, supplied by the caller from the
    #: durable reports. It is how a carried seat can say HOW OLD it is
    #: instead of only that it is carried. A seat missing from here is a
    #: carried seat of unknown age, and says so rather than guessing.
    prior_reads: dict[str, dict] = field(default_factory=dict)

    @property
    def seats(self) -> int:
        return len(self.data_status)

    @property
    def summary(self) -> str:
        """Machine-side one-liner for the durable record and the log."""
        parts = [
            f"{len(self.fresh)} of {self.seats} research seat(s) were read on "
            f"this tick ({', '.join(self.fresh) or 'none'})",
            f"carried from earlier without being re-read: {', '.join(self.carried) or 'none'}",
            f"no answer at all: {', '.join(self.absent) or 'none'}",
        ]
        if self.known_out_of_date:
            parts.append("carried answers the desk knows are superseded: " + ", ".join(self.known_out_of_date))
        if self.unknown:
            parts.append(
                "seats whose state this desk cannot classify (NOT counted as read): " + ", ".join(self.unknown)
            )
        return "; ".join(parts)

    def stamped(self, *, run_id=None, mode=None, stamped_at=None, prior_reads=None) -> "EvidenceFreshness":
        """Return the same classification carrying WHEN and WHICH RUN.

        A separate step from `freshness()` because the classification is
        computed deep in the gate, where the run identity is not in hand,
        while the caller that persists the row has both. Never raises and
        never changes a seat's bucket — it only attaches provenance.
        """
        from datetime import datetime, timezone

        if stamped_at is None:
            from src.trading_calendar import utc_now

            stamped_at = utc_now().isoformat()
        clean_prior: dict[str, dict] = {}
        if isinstance(prior_reads, dict):
            for seat, entry in prior_reads.items():
                if isinstance(entry, dict):
                    clean_prior[str(seat)] = dict(entry)
        return replace(
            self,
            run_id=None if run_id is None else str(run_id),
            mode=None if mode is None else str(mode),
            stamped_at=str(stamped_at),
            prior_reads=clean_prior,
        )

    def seat_stamps(self) -> dict:
        """Per seat: which of the THREE states it is in, and its provenance.

        The three are kept apart on purpose and are never collapsed:
        `refreshed_this_session` (this run read it), `carried_forward`
        (the desk holds an older answer, with `age_seconds` when the
        earlier run is known and None when it is not), and `absent` (no
        usable answer at all — which is NOT staleness). `unknown` is a
        fourth, reserved for a status this desk cannot classify, and is
        never reported as a read.

        No threshold lives here. Nothing in this method decides anything.
        """
        from datetime import datetime

        def _age(then: str | None) -> int | None:
            if not then:
                return None
            try:
                start = datetime.fromisoformat(str(then))
                end = datetime.fromisoformat(str(self.stamped_at))
            except (TypeError, ValueError):
                return None
            if start.tzinfo is None or end.tzinfo is None:
                start = start.replace(tzinfo=None)
                end = end.replace(tzinfo=None)
            return int((end - start).total_seconds())

        stamps: dict[str, dict] = {}
        for seat in self.fresh:
            stamps[seat] = {
                "state": READ_REFRESHED,
                "run_id": self.run_id,
                "mode": self.mode,
                "at": self.stamped_at,
                # Zero by construction, not by a literal: this module is
                # held to carrying no numeric constant at all.
                "age_seconds": _age(self.stamped_at),
            }
        for seat in self.carried:
            prior = self.prior_reads.get(seat) or {}
            at = prior.get("at")
            stamps[seat] = {
                "state": READ_CARRIED,
                "run_id": prior.get("run_id"),
                "mode": prior.get("mode"),
                "at": at,
                "age_seconds": _age(at),
            }
        for seat in self.absent:
            stamps[seat] = {
                "state": READ_ABSENT,
                "run_id": None,
                "mode": None,
                "at": None,
                "age_seconds": None,
            }
        for seat in self.unknown:
            stamps[seat] = {
                "state": READ_UNKNOWN,
                "run_id": None,
                "mode": None,
                "at": None,
                "age_seconds": None,
            }
        return stamps

    def to_evidence(self) -> dict:
        return {
            "stamped_run_id": self.run_id,
            "stamped_mode": self.mode,
            "stamped_at": self.stamped_at,
            "seat_stamps": self.seat_stamps(),
            "fresh_seats": list(self.fresh),
            "carried_seats": list(self.carried),
            "absent_seats": list(self.absent),
            "unknown_freshness_seats": list(self.unknown),
            "known_out_of_date_seats": list(self.known_out_of_date),
            "seats_total": self.seats,
            "seats_read_this_tick": len(self.fresh),
            "summary": self.summary,
        }


class FreshnessReader:
    """Classifies a `data_status` mapping using the tables it was handed."""

    def __init__(
        self,
        *,
        status_freshness,
        expired_statuses,
        fresh_label,
        carried_label,
        absent_label,
    ) -> None:
        self._status_freshness = dict(status_freshness)
        self._expired_statuses = frozenset(str(s) for s in expired_statuses)
        self._fresh_label = fresh_label
        self._carried_label = carried_label
        self._absent_label = absent_label

    def read(self, data_status: dict | None) -> EvidenceFreshness:
        """Classify each seat by whether its answer was read on THIS tick.

        NEVER raises, and never counts an unrecognised state as fresh: an
        unknown word means the desk does not know how fresh that seat is, and
        claiming freshness it cannot prove is the failure this exists to stop.
        """
        if not isinstance(data_status, dict):
            return EvidenceFreshness()
        fresh: list[str] = []
        carried: list[str] = []
        absent: list[str] = []
        unknown: list[str] = []
        stale: list[str] = []
        clean: dict[str, str] = {}
        for seat, value in data_status.items():
            seat_name = str(seat)
            text = str(value)
            clean[seat_name] = text
            bucket = self._status_freshness.get(text)
            if bucket == self._fresh_label:
                fresh.append(seat_name)
            elif bucket == self._carried_label:
                carried.append(seat_name)
                if text in self._expired_statuses:
                    stale.append(seat_name)
            elif bucket == self._absent_label:
                absent.append(seat_name)
            else:
                unknown.append(seat_name)
                logger.error(
                    "evidence freshness: data_status[%r]=%r is not in "
                    "the freshness table it was built with — reporting it as "
                    "unknown freshness, NOT as read-this-tick. Classify it.",
                    seat_name,
                    text,
                )
        return EvidenceFreshness(
            fresh=sorted(fresh),
            carried=sorted(carried),
            absent=sorted(absent),
            unknown=sorted(unknown),
            known_out_of_date=sorted(stale),
            data_status=clean,
        )


def build_freshness_reader(
    *,
    status_freshness,
    expired_statuses,
    fresh_label,
    carried_label,
    absent_label,
) -> FreshnessReader:
    """Build the reader from plain values — it reaches back into nothing.

    Nothing here has a default. The status vocabulary is the desk's own and
    a reader that quietly invented one would report seats as fresh that
    nobody classified, which is the exact failure `read` exists to stop.
    """
    return FreshnessReader(
        status_freshness=status_freshness,
        expired_statuses=expired_statuses,
        fresh_label=fresh_label,
        carried_label=carried_label,
        absent_label=absent_label,
    )
