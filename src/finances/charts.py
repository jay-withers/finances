"""Chart geometry for the insights page, drawn as inline SVG by the templates.

Server-side and dependency-free: the figures are a dozen months, a charting
library would be the biggest thing on the page, and a CDN script would need a
CSP exception. Here a chart is plain numbers in viewBox units, and the
template draws them. The only script is the tooltip, which is optional: every
chart has a table under it with the same figures.

Coordinates are floats, which is fine: they are positions on screen, never
money. The money they label stays integer pence and is formatted by
`money.format_money` or `compact`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from .calc import MonthFlow
from .money import format_money

Format = Callable[[int], str]

# The viewBox. The SVG stretches to the card (`preserveAspectRatio="none"`,
# strokes non-scaling), and every label is HTML placed over it by percentage,
# so text stays the page's size on a phone and on a laptop alike.
WIDTH = 640
HEIGHT = 200
LEFT = 60  # a gutter for the tick labels, so they never sit on a mark
RIGHT = 4
TOP = 10  # half a tick label, which is centred on its gridline
BOTTOM = 4

# Columns are capped rather than filling their slot, with air either side.
MAX_BAR = 24


def compact(pence: int) -> str:
    """An axis label: `£0`, `£750`, `£1.5k`, `£12k`, `£1.2m`. Whole pounds only."""
    sign = "-" if pence < 0 else ""
    pounds = abs(pence) // 100
    if pounds < 1_000:
        return f"{sign}£{pounds}"
    # 999,500 would round to "1000k", so it is already a million.
    size, suffix = (1_000_000, "m") if pounds >= 999_500 else (1_000, "k")
    whole, fraction = divmod((pounds * 10 + size // 2) // size, 10)
    if whole >= 100 or not fraction:
        return f"{sign}£{(pounds + size // 2) // size}{suffix}"
    return f"{sign}£{whole}.{fraction}{suffix}"


def nice_ticks(low: int, high: int, count: int = 4) -> list[int]:
    """Round tick values in pence spanning `low`..`high`, zero included.

    Steps are 1, 2, 2.5 or 5 times a power of ten pounds, the smallest that
    covers the range in at most `count` intervals.
    """
    low, high = min(low, 0), max(high, 0)
    if low == high:
        high = 10_000  # an empty chart still gets an axis: £0 to £100
    magnitude = 100  # £1
    while True:
        # No £2.50 steps: the labels are whole pounds, so they'd read £2, £5, £7.
        for multiple in (10, 20, 50) if magnitude == 100 else (10, 20, 25, 50):
            step = magnitude * multiple // 10
            first = (low // step) * step
            last = -(-high // step) * step
            if (last - first) // step <= count:
                return list(range(first, last + 1, step))
        magnitude *= 10


@dataclass(frozen=True)
class Tick:
    y: float
    label: str

    @property
    def percent(self) -> float:
        """Its height as a percentage of the chart's, for the HTML label."""
        return round(self.y * 100 / HEIGHT, 2)


@dataclass(frozen=True)
class Slot:
    """One month's column of the chart: where it sits, and what hovering says."""

    month: date
    x: float  # centre
    left: float  # the hover target spans the whole slot
    width: float
    label: str  # the month, shown under every other slot on a phone-width chart
    # where it sits, as a percentage of the chart's width, for the HTML labels
    percent: float
    show_label: bool
    tip: str


@dataclass(frozen=True)
class Point:
    x: float
    y: float

    @property
    def left(self) -> float:
        return round(self.x * 100 / WIDTH, 2)

    @property
    def top(self) -> float:
        return round(self.y * 100 / HEIGHT, 2)


@dataclass(frozen=True)
class Bar:
    x: float
    y: float
    width: float
    height: float
    # A path, so the data end is rounded and the baseline end square.
    path: str


@dataclass(frozen=True)
class Chart:
    ticks: list[Tick]
    slots: list[Slot]
    baseline: float  # y of £0
    line: str = ""  # an SVG path, for the balance chart
    area: str = ""  # the wash under the line, closed to the baseline
    points: tuple[Point, ...] = ()
    paid_in: tuple[Bar, ...] = ()
    paid_out: tuple[Bar, ...] = ()


def _scale(ticks: list[int]) -> Callable[[int], float]:
    low, high = ticks[0], ticks[-1]
    plot = HEIGHT - TOP - BOTTOM

    def y(pence: int) -> float:
        return round(TOP + (high - pence) * plot / (high - low), 2)

    return y


def _slots(flows: list[MonthFlow], tips: list[str]) -> list[Slot]:
    width = (WIDTH - LEFT - RIGHT) / max(len(flows), 1)
    # Twelve labels don't fit a phone; every other one does, and the newest
    # month is always labelled.
    newest = len(flows) - 1
    return [
        Slot(
            month=flow.month,
            x=round(LEFT + width * (i + 0.5), 2),
            left=round(LEFT + width * i, 2),
            width=round(width, 2),
            label=flow.month.strftime("%b"),
            percent=round((LEFT + width * (i + 0.5)) * 100 / WIDTH, 2),
            show_label=(newest - i) % 2 == 0,
            tip=tip,
        )
        for i, (flow, tip) in enumerate(zip(flows, tips, strict=True))
    ]


def _column(x: float, top: float, bottom: float, width: float, up: bool) -> Bar:
    """A column from the baseline, its far end rounded (4 units) and its
    baseline end square."""
    height = bottom - top
    left = x - width / 2
    r = min(4.0, width / 2, height)
    if height <= 0:
        return Bar(left, top, width, 0, "")
    right = left + width
    # `base` is the square end on the baseline, `end` the rounded data end,
    # and `inward` the direction from the data end back toward the baseline.
    base, end, inward = (bottom, top, r) if up else (top, bottom, -r)
    path = (
        f"M{left:.2f},{base:.2f} V{end + inward:.2f} "
        f"Q{left:.2f},{end:.2f} {left + r:.2f},{end:.2f} H{right - r:.2f} "
        f"Q{right:.2f},{end:.2f} {right:.2f},{end + inward:.2f} V{base:.2f} Z"
    )
    return Bar(round(left, 2), round(top, 2), round(width, 2), round(height, 2), path)


def balance_chart(flows: list[MonthFlow], fmt: Format = format_money) -> Chart:
    """Every pot's balance at each month's end, as one line."""
    values = [f.closing for f in flows]
    ticks = nice_ticks(min(values, default=0), max(values, default=0))
    y = _scale(ticks)
    slots = _slots(flows, [f"{f.month:%B %Y}: {fmt(f.closing)}" for f in flows])
    points = tuple(Point(s.x, y(v)) for s, v in zip(slots, values, strict=True))
    line = " ".join(f"{'M' if i == 0 else 'L'}{p.x},{p.y}" for i, p in enumerate(points))
    # The wash fills down to the axis floor: £0, or the lowest tick when the
    # balance has been below zero.
    base = y(ticks[0])
    area = f"{line} L{points[-1].x},{base} L{points[0].x},{base} Z" if points else ""
    return Chart(
        ticks=[Tick(y(t), compact(t)) for t in ticks],
        slots=slots,
        baseline=y(0),
        line=line,
        area=area,
        points=points,
    )


def flow_chart(flows: list[MonthFlow], fmt: Format = format_money) -> Chart:
    """Money in above the line and money out below it, one pair per month.

    One axis, one scale: out is drawn as a negative so the two are compared on
    the same ruler, not two charts that look alike but measure differently.
    """
    ticks = nice_ticks(
        min((f.paid_out for f in flows), default=0), max((f.paid_in for f in flows), default=0)
    )
    y = _scale(ticks)
    zero = y(0)
    tips = [
        f"{f.month:%B %Y}: in {fmt(f.paid_in)}, out {fmt(-f.paid_out)}, net {fmt(f.net)}"
        for f in flows
    ]
    slots = _slots(flows, tips)
    width = min(MAX_BAR, slots[0].width * 0.6) if slots else MAX_BAR
    return Chart(
        ticks=[Tick(y(t), compact(t)) for t in ticks],
        slots=slots,
        baseline=zero,
        # A 2-unit gap at the baseline keeps the pair apart where they meet.
        paid_in=tuple(
            _column(s.x, y(f.paid_in), zero - 1, width, up=True)
            for s, f in zip(slots, flows, strict=True)
        ),
        paid_out=tuple(
            _column(s.x, zero + 1, y(f.paid_out), width, up=False)
            for s, f in zip(slots, flows, strict=True)
        ),
    )
