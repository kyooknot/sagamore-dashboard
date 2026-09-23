#!/usr/bin/env python3
"""your town recycling schedule — pure date logic.

No I/O, no network, no framework. Everything here is deterministic given a
calendar date, so the validation/sanity tables in ``../CLAUDE.md`` exercise this
module directly. ``app.py`` wraps it for HTTP; ``detector.py`` reuses the pinned
constants. See that spec for the rules these functions implement.

The recycling cycle is a fixed weekly alternation anchored to a verified paper
Wednesday. Holidays are the six-entry OBSERVED_HOLIDAYS rule set (the source of
truth — never read from the calendar image). A holiday on the Mon/Tue/Wed of the
pickup week shifts the Wednesday pickup to Thursday; the recycling *type* never
changes on a shift.
"""

from __future__ import annotations

from datetime import date, timedelta

# === EDIT HERE — schedule constants (the only things you touch) ==============
#
# Anchor: a *verified* paper Wednesday. Whole weeks away from this date that are
# even => paper, odd => containers. 2025-07-02 was containers (the week before),
# which is consistent with this parity. Both were confirmed against the printed
# calendar (documentID 18667). If the printed calendar ever disagrees, the
# calendar wins — change this anchor, not the algorithm.
ANCHOR_PAPER = date(2025, 7, 9)

# Last pickup covered by the published calendar. Pickups past this date still
# compute, but carry a "verify" note so we know to refresh the anchor.
#
# Extended 2026-09-02 to the "2026-2027 REVISED Trash & Recycling Collection"
# calendar (your town documentID 20181), which runs July 2026 - June 2027.
# The alternation was verified against the printed grid rather than assumed --
# the anchor is unchanged, so the parity carried straight through the gap:
#   Wed 2026-09-02  blue   -> paper       Wed 2026-12-02  orange -> containers
#   Wed 2026-12-23  blue   -> paper       Wed 2027-06-02  orange -> containers
#   Wed 2027-06-30  orange -> containers
# Circled holidays on that calendar (Sep 7, Nov 26, Dec 25, Jan 1, May 31) are all
# already produced by OBSERVED_HOLIDAYS. Note Christmas 2026 is a FRIDAY, so it
# correctly causes no Wednesday shift -- the calendar moves Friday's pickup to
# Saturday the 26th instead, which this module does not model and does not need to.
SCHEDULE_END = date(2027, 6, 30)

# weekday() values (Mon=0 .. Sun=6)
MONDAY = 0
WEDNESDAY = 2
THURSDAY = 3


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The ``n``-th given weekday in a month (n=1 => first)."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """The last given weekday in a month."""
    if month == 12:
        nxt = date(year + 1, 1, 1)
    else:
        nxt = date(year, month + 1, 1)
    last_day = nxt - timedelta(days=1)
    offset = (last_day.weekday() - weekday) % 7
    return last_day - timedelta(days=offset)


# Observed collection holidays (your town). Transcribed from the spec, NOT
# read from the calendar image. Each entry computes the date for any year, so the
# Mon/Tue/Wed shift test stays correct as holidays drift across weekdays.
OBSERVED_HOLIDAYS = {
    "New Year's Day":   lambda y: date(y, 1, 1),
    "Memorial Day":     lambda y: _last_weekday(y, 5, MONDAY),       # last Mon, May
    "Independence Day": lambda y: date(y, 7, 4),
    "Labor Day":        lambda y: _nth_weekday(y, 9, MONDAY, 1),     # 1st Mon, Sep
    "Thanksgiving":     lambda y: _nth_weekday(y, 11, THURSDAY, 4),  # 4th Thu, Nov
    "Christmas":        lambda y: date(y, 12, 25),
}

# === end editable constants ==================================================

TYPE_LABELS = {
    "paper": "Paper & Cardboard",
    "containers": "Cans, Bottles & Glass",
}


def recycling_type(pickup_wednesday: date) -> str:
    """``"paper"`` or ``"containers"`` for a pickup-week Wednesday.

    Computed in whole calendar days // 7 (not timestamps) so it can't drift
    across a DST boundary. Even weeks from the paper anchor are paper.
    """
    weeks = (pickup_wednesday - ANCHOR_PAPER).days // 7
    return "paper" if weeks % 2 == 0 else "containers"


def wednesday_of_week(d: date) -> date:
    """The Wednesday of the Mon–Sun week containing ``d``."""
    monday = d - timedelta(days=d.weekday())
    return monday + timedelta(days=WEDNESDAY)


def holiday_shift(wednesday: date) -> tuple[bool, str | None]:
    """Does a holiday land on Mon/Tue/Wed of this pickup week?

    Returns ``(shifted, reason)``. A Thu/Fri/weekend holiday never lands on
    Mon–Wed, so it produces no shift. The week can straddle a year boundary
    (late Dec / early Jan), so holidays are checked for every year present.
    """
    monday = wednesday - timedelta(days=2)
    week_mon_to_wed = {monday, monday + timedelta(days=1), wednesday}
    years = {monday.year, wednesday.year}
    for name, rule in OBSERVED_HOLIDAYS.items():
        for y in years:
            if rule(y) in week_mon_to_wed:
                return True, name
    return False, None


def pickup_for_week(wednesday: date) -> dict:
    """Resolve type, holiday shift, and actual pickup date for one week."""
    shifted, reason = holiday_shift(wednesday)
    actual = wednesday + timedelta(days=1) if shifted else wednesday
    return {
        "type_short": recycling_type(wednesday),  # type is keyed on the Wednesday
        "shifted": shifted,
        "shift_reason": reason,
        "pickup_date": actual,
        "pickup_day": "Thursday" if shifted else "Wednesday",
    }


def current_pickup(today: date) -> dict:
    """The upcoming/current pickup, rolling forward once this week's has passed."""
    wednesday = wednesday_of_week(today)
    week = pickup_for_week(wednesday)
    if today > week["pickup_date"]:
        week = pickup_for_week(wednesday + timedelta(days=7))
    return week


def build_payload(today: date, *, source_changed: bool = False) -> dict:
    """The flat JSON contract Homepage's customapi widget reads."""
    week = current_pickup(today)
    note = ""
    if week["pickup_date"] > SCHEDULE_END:
        note = "Schedule beyond 2026-06-30 unverified"
    if source_changed:
        # A flagged source change takes precedence in the single note slot.
        note = "Source calendar changed — verify schedule"
    return {
        "type": TYPE_LABELS[week["type_short"]],
        "type_short": week["type_short"],
        "pickup_day": week["pickup_day"],
        "pickup_date": week["pickup_date"].isoformat(),
        "shifted": week["shifted"],
        "shift_reason": week["shift_reason"],
        "source_changed": source_changed,
        "note": note,
    }
