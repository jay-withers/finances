"""The insights charts' geometry: ticks, labels and where the marks land."""

from __future__ import annotations

from datetime import date
from itertools import pairwise

from hypothesis import given
from hypothesis import strategies as st

from finances import charts
from finances.calc import MonthFlow


def flows(*rows: tuple[int, int, int]) -> list[MonthFlow]:
    return [
        MonthFlow(month=date(2026, i + 1, 1), paid_in=a, paid_out=b, closing=c)
        for i, (a, b, c) in enumerate(rows)
    ]


def test_compact_labels():
    cases = {
        0: "£0",
        75_000: "£750",
        100_000: "£1k",
        150_000: "£1.5k",
        1_234_500: "£12.3k",
        9_996_000: "£100k",  # not "£100.0k"
        99_960_000: "£1m",  # not "£1000k"
        120_000_000: "£1.2m",
        -45_000: "-£450",
        -150_000: "-£1.5k",
    }
    assert {pence: charts.compact(pence) for pence in cases} == cases


def test_nice_ticks_include_zero_and_cover_the_range():
    assert charts.nice_ticks(-30_000, 127_500) == [-50_000, 0, 50_000, 100_000, 150_000]
    assert charts.nice_ticks(0, 850_000) == [0, 250_000, 500_000, 750_000, 1_000_000]


def test_no_step_shows_pence_the_labels_would_drop():
    """A £2.50 step labelled in whole pounds read £0, £2, £5, £7, £10."""
    ticks = charts.nice_ticks(0, 900)
    assert all(t % 100 == 0 for t in ticks)
    assert [charts.compact(t) for t in ticks] == ["£0", "£5", "£10"]


def test_an_empty_chart_still_gets_an_axis():
    assert charts.nice_ticks(0, 0) == [0, 2_500, 5_000, 7_500, 10_000]


@given(st.integers(-(10**10), 10**10), st.integers(-(10**10), 10**10))
def test_ticks_always_span_the_data_and_zero_in_a_few_even_steps(a: int, b: int):
    low, high = min(a, b), max(a, b)
    ticks = charts.nice_ticks(low, high)
    assert ticks[0] <= min(low, 0) and ticks[-1] >= max(high, 0)
    assert 0 in ticks
    assert 2 <= len(ticks) <= 5
    assert all(t % 100 == 0 for t in ticks)  # whole pounds, as `compact` shows them
    assert len({b - a for a, b in pairwise(ticks)}) == 1


def test_the_balance_line_runs_through_every_month_top_to_bottom():
    chart = charts.balance_chart(flows((0, 0, 100_000), (0, 0, 0), (0, 0, 50_000)))
    ys = [p.y for p in chart.points]
    assert len(chart.points) == 3
    assert ys[0] < ys[2] < ys[1]  # higher values sit higher up the chart
    assert ys[1] == chart.baseline  # £0 sits on the zero line
    assert chart.line.startswith("M") and chart.line.count("L") == 2
    assert chart.slots[0].tip == "January 2026: £1,000"


def test_in_rises_from_the_baseline_and_out_falls_from_it():
    chart = charts.flow_chart(flows((30_000, -10_000, 0), (0, 0, 0)))
    up, down = chart.paid_in[0], chart.paid_out[0]
    assert up.y + up.height < chart.baseline < down.y
    assert up.height > down.height  # £300 against £100 on the same scale
    assert up.width <= charts.MAX_BAR
    # A month with nothing draws nothing, rather than a sliver.
    assert chart.paid_in[1].path == "" and chart.paid_out[1].path == ""
    assert chart.slots[0].tip == "January 2026: in £300, out £100, net £200"


def test_every_other_month_is_labelled_ending_with_the_newest():
    chart = charts.balance_chart(flows(*[(0, 0, 0)] * 12))
    shown = [s.label for s in chart.slots if s.show_label]
    assert shown == ["Feb", "Apr", "Jun", "Aug", "Oct", "Dec"]
    assert all(0 < s.percent < 100 for s in chart.slots)


def test_tick_and_point_positions_are_percentages_of_the_chart():
    chart = charts.balance_chart(flows((0, 0, 100_000)))
    assert all(0 <= t.percent <= 100 for t in chart.ticks)
    point = chart.points[0]
    assert 0 < point.left < 100 and 0 <= point.top <= 100
