"""The core value types.

The whole reason this project exists rather than another Homepage config is
here: a dashboard that shows `off` for a sensor that stopped reporting three
weeks ago is worse than no dashboard, because it manufactures confidence.

So every value carries **when it was last actually observed**, and every tile
resolves to one of four states — not two:

    OK       the thing is fine, and we know that recently
    WARN     the thing needs attention
    ALERT    the thing is wrong now
    UNKNOWN  we cannot currently see it  ← never rendered as "fine"

That last state is the one Homepage doesn't have, and it is exactly the failure
mode that hid the dead sump-pump alert for months: Zigbee kept serving the last
retained value, so the tile looked healthy while the sensor was gone.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class Status(str, Enum):
    OK = "ok"
    WARN = "warn"
    ALERT = "alert"
    UNKNOWN = "unknown"

    @property
    def rank(self) -> int:
        return {"ok": 0, "unknown": 1, "warn": 2, "alert": 3}[self.value]


def worst(*statuses: "Status") -> Status:
    """Roll child statuses up to a parent. UNKNOWN outranks OK deliberately."""
    live = [s for s in statuses if s is not None]
    if not live:
        return Status.UNKNOWN
    return max(live, key=lambda s: s.rank)


@dataclass
class Reading:
    """One observed value, plus how stale it is allowed to get.

    `max_age` is what turns a number into a judgement. A garage-door state that
    is 6 hours old is fine; a sump-pump power reading that is 6 hours old means
    the sensor is gone and the alert that depends on it cannot fire.
    """

    label: str
    value: object = None
    unit: str = ""
    as_of: float | None = None          # epoch seconds of last real observation
    max_age: float | None = None        # seconds before the reading is untrustworthy
    status: Status = Status.OK
    note: str = ""
    href: str = ""
    # A value established as *unobtainable* is a documented fact, not a blind
    # spot: PS5 storage is not exposed by any interface, so rendering it as
    # UNKNOWN would imply we might one day see it and currently cannot. Such a
    # reading is displayed but excluded from the panel roll-up.
    informational: bool = False
    # Which block of the card this belongs in. "" is the main list; a named group
    # ("maint", "score", "pool", "host") lets the template collapse or lay out a
    # subset without the panel having to know how it will be drawn. Grouping is
    # presentation only -- every reading still scores into the panel roll-up, so
    # an overdue filter tucked inside a collapsed section still colours the card.
    group: str = ""
    # Presentation hints, same contract as `group`: the panel states them, the template
    # decides what they look like, and neither affects the roll-up.
    #   mark  a small unit glyph drawn after the value ("ra", "g", "trophy")
    #   bar   0-100, drawn as a fill across the row so a capacity reads at a glance
    #   trend "up" / "down" — a direction arrow drawn before the note. Up is drawn
    #         alert-coloured because up is the one that costs money, matching the
    #         per-circuit trend column. The glyph carries the direction as well as
    #         the colour, so it survives a colour-blind reader and a greyscale print.
    mark: str = ""
    bar: float | None = None
    trend: str = ""

    @property
    def age(self) -> float | None:
        if self.as_of is None:
            return None
        return max(0.0, time.time() - self.as_of)

    @property
    def stale(self) -> bool:
        if self.max_age is None or self.as_of is None:
            return False
        return self.age > self.max_age

    @property
    def missing(self) -> bool:
        return self.value in (None, "", "unavailable", "unknown")

    @property
    def effective_status(self) -> Status:
        """Staleness and absence always win over the caller's optimism."""
        if self.informational:
            return Status.OK
        if self.missing or self.stale:
            return Status.UNKNOWN
        return self.status

    @property
    def age_text(self) -> str:
        a = self.age
        if a is None:
            return ""
        return humanise_age(a)

    def display(self) -> str:
        if self.missing:
            return "—"
        v = self.value
        if isinstance(v, float):
            v = f"{v:,.1f}".rstrip("0").rstrip(".") if abs(v) < 1000 else f"{v:,.0f}"
        return f"{v}{(' ' + self.unit) if self.unit else ''}"


def humanise_age(seconds: float) -> str:
    s = int(seconds)
    if s < 90:
        return f"{s}s ago"
    m = s // 60
    if m < 90:
        return f"{m}m ago"
    h = m // 60
    if h < 48:
        return f"{h}h ago"
    return f"{h // 24}d ago"


@dataclass
class Panel:
    """A dashboard section. `headline` is the one thing worth reading."""

    key: str
    title: str
    headline: str = ""
    sub: str = ""
    status: Status = Status.OK
    readings: list[Reading] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    # A second, independent table. The homelab panel needs two: apt/patch debt
    # per node, and self-hosted app versions — different shapes, different
    # sources, different freshness. Overloading `rows` with a discriminator
    # would make the template guess.
    app_rows: list[dict] = field(default_factory=list)
    # And a third, for the same reason: the homelab card already spends `rows` on patch
    # debt per node and `app_rows` on self-hosted versions. Devices that have stopped
    # answering are a fourth shape from a fourth source.
    dead_rows: list[dict] = field(default_factory=list)
    error: str = ""
    configured: bool = True

    def rollup(self) -> Status:
        if not self.configured:
            return Status.UNKNOWN
        if self.error:
            return Status.UNKNOWN
        scoring = [r for r in self.readings if not r.informational]
        computed = worst(*[r.effective_status for r in scoring]) if scoring else Status.OK
        return worst(computed, self.status)
