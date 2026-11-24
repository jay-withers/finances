"""The derived values that replace the spreadsheet's formulas."""

from __future__ import annotations

import math
from datetime import date, timedelta
from itertools import pairwise

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from finances import calc
from finances.model import (
    PURPOSES,
    Document,
    Line,
    PaydayRun,
    Pot,
    PotEntry,
    Renewal,
    Transfer,
    WealthAccount,
    WealthSnapshot,
)
from finances.settings import settings


def test_pot_balance_is_the_sum_of_its_ledger(doc: Document):
    car = next(p for p in doc.pots if p.name == "Car")
    # 20000 in, 30000 out.
    assert calc.pot_balance(doc, car.id) == -10_000


def test_pot_balances_covers_every_pot(doc: Document):
    balances = calc.pot_balances(doc)
    assert set(balances) == {p.id for p in doc.pots}
    assert sum(balances.values()) == calc.pots_total(doc)


def test_ledger_months_groups_newest_first_with_totals(doc: Document):
    car = next(p for p in doc.pots if p.name == "Car")
    doc.pot_entries += [
        PotEntry(pot_id=car.id, on=date(2026, 9, 28), amount_pence=5_000, kind="payday"),
        PotEntry(pot_id=car.id, on=date(2026, 11, 3), amount_pence=1_000, kind="deposit"),
    ]
    months = calc.ledger_months(doc.entries_for(car.id))

    # October had nothing, so it isn't listed.
    assert [m.month for m in months] == [date(2026, 11, 1), date(2026, 9, 1), date(2026, 8, 1)]
    nov, sep, aug = months
    assert (sep.paid_in, sep.paid_out, sep.net, sep.imported) == (5_000, -30_000, -25_000, 0)
    assert [e.on for e in sep.entries] == [date(2026, 9, 28), date(2026, 9, 2)]
    # The import is not money moved that month, but it is in the balance.
    assert (aug.paid_in, aug.imported, aug.closing) == (0, 20_000, 20_000)
    assert sep.closing == -5_000
    assert nov.closing == calc.pot_balance(doc, car.id)


def test_a_correction_is_not_counted_as_saving(doc: Document):
    car = next(p for p in doc.pots if p.name == "Car")
    doc.pot_entries.append(
        PotEntry(pot_id=car.id, on=date(2026, 9, 30), amount_pence=30_000, kind="adjustment")
    )
    sep = calc.ledger_months(doc.entries_for(car.id))[0]
    assert (sep.paid_in, sep.paid_out, sep.adjusted, sep.closing) == (0, -30_000, 30_000, 20_000)


def test_archiving_a_pot_does_not_rewrite_past_months(doc: Document):
    before = calc.pots_ledger_months(doc)
    doc.pots[1].archived = True  # Car, which spent in September
    assert calc.pots_ledger_months(doc) == before


def test_pots_ledger_months_leaves_out_entries_for_a_deleted_pot(doc: Document):
    doc.pot_entries.append(PotEntry(pot_id="gone", on=date(2026, 12, 1), amount_pence=1))
    assert calc.pots_ledger_months(doc)[0].month == date(2026, 9, 1)


def test_ledger_months_of_nothing_is_empty():
    assert calc.ledger_months([]) == []


def test_attention_says_one_day_not_one_days(doc: Document):
    details = [i.detail for i in calc.attention(doc, date(2026, 11, 7)).items]
    assert "expired 1 day ago" in details
    details = [i.detail for i in calc.attention(doc, date(2026, 11, 5)).items]
    assert "in 1 day" in details


def test_no_payday_nag_when_no_pot_would_be_topped_up(doc: Document):
    for pot in doc.pots:
        pot.archived = True
    items = calc.attention(doc, date(2026, 9, 29)).items
    assert not [i for i in items if i.href == "/payday"]


def test_stale_wealth_is_the_oldest_valuation_past_the_limit(doc: Document):
    # The oldest is the private pension, valued 29 Apr; the limit is 90 days.
    stale = calc.stale_wealth(doc, date(2026, 9, 20))
    assert stale is not None
    account, _snapshot, age = stale
    assert (account.company, age) == ("A Pension", 144)
    assert calc.stale_wealth(doc, date(2026, 5, 1)) is None


def test_next_renewal_skips_expired_and_rolling(doc: Document):
    assert calc.next_renewal(doc, date(2026, 11, 6)) is not None
    renewal, days = calc.next_renewal(doc, date(2026, 11, 6)) or (None, None)
    assert (renewal and renewal.kind, days) == ("Car MOT", 0)  # today counts
    renewal, days = calc.next_renewal(doc, date(2026, 11, 7)) or (None, None)
    assert (renewal and renewal.kind, days) == ("Mortgage", 205)
    assert calc.next_renewal(doc, date(2027, 6, 1)) is None  # only the rolling SIM left


# --- insights -----------------------------------------------------------------


def test_month_starts_cross_a_year_end():
    assert calc.month_starts(date(2026, 2, 17), 3) == [
        date(2025, 12, 1),
        date(2026, 1, 1),
        date(2026, 2, 1),
    ]


def test_monthly_flows_fill_empty_months_and_carry_the_balance(doc: Document):
    # The doc: openings on 31 Aug (147500 + 630000 across pots), a 30000 spend
    # on 2 Sep. Nothing in October or November.
    flows = calc.monthly_flows(doc, date(2026, 11, 5), months=4)
    assert [f.month for f in flows] == [date(2026, m, 1) for m in (8, 9, 10, 11)]
    assert [f.paid_in for f in flows] == [0, 0, 0, 0]  # an import isn't money in
    assert [f.paid_out for f in flows] == [0, -30_000, 0, 0]
    assert [f.closing for f in flows] == [777_500, 747_500, 747_500, 747_500]


def test_monthly_flows_start_from_the_balance_before_the_window(doc: Document):
    flows = calc.monthly_flows(doc, date(2027, 1, 5), months=2)
    assert [f.closing for f in flows] == [747_500, 747_500]


def test_average_rounds_to_the_nearest_penny_either_side_of_zero():
    assert calc.average(1_000, 12) == 83  # 83.33
    assert calc.average(1_010, 12) == 84  # 84.17
    assert calc.average(-1_010, 12) == -84
    assert calc.average(6, 12) == 1  # half rounds away from zero
    assert calc.average(-6, 12) == -1


def test_pot_health_verdicts_worst_first(doc: Document):
    holidays, _car, general = doc.pots
    today = date(2026, 11, 5)
    doc.pot_entries += [
        # Holidays: topped up 450 a month for the last two full months.
        PotEntry(pot_id=holidays.id, on=date(2026, 9, 28), amount_pence=45_000, kind="payday"),
        PotEntry(pot_id=holidays.id, on=date(2026, 10, 28), amount_pence=45_000, kind="payday"),
        # Savings: 1200 out over its three months, so 400 a month.
        PotEntry(pot_id=general.id, on=date(2026, 10, 2), amount_pence=-120_000, kind="spend"),
        # This month isn't judged yet: a big spend here changes nothing.
        PotEntry(pot_id=holidays.id, on=date(2026, 11, 2), amount_pence=-500_000, kind="spend"),
    ]
    health = calc.pot_health(doc, today)
    by_name = {h.pot.name: h for h in health}

    assert [h.pot.name for h in health] == ["Holidays", "Car", "Savings"]
    # Holidays is overdrawn by this month's spend, though its months were good.
    assert by_name["Holidays"].verdict == "overdrawn"
    # The doc's pots opened on 31 August, so they have three full months.
    assert by_name["Holidays"].months_of_history == 3
    assert by_name["Holidays"].average_in == 30_000  # 90000 / 3
    assert by_name["Car"].verdict == "overdrawn"
    savings = by_name["Savings"]
    assert savings.average_out == -40_000
    assert savings.verdict == "running down"
    assert savings.months_left == 13  # 510000 / 40000, rounded up


def test_a_young_pot_is_averaged_over_the_months_it_has_existed():
    """Two months of spending spread over twelve said it would last six times
    longer than it will."""
    pot = Pot(name="New")
    doc = Document(
        pots=[pot],
        pot_entries=[
            PotEntry(pot_id=pot.id, on=date(2026, 9, 1), amount_pence=60_000, kind="opening"),
            PotEntry(pot_id=pot.id, on=date(2026, 9, 15), amount_pence=-30_000, kind="spend"),
            PotEntry(pot_id=pot.id, on=date(2026, 10, 15), amount_pence=-20_000, kind="spend"),
        ],
    )
    (health,) = calc.pot_health(doc, date(2026, 11, 5))
    assert (health.months_of_history, health.average_out) == (2, -25_000)
    assert health.months_left == 1  # 10000 left at 25000 a month


def test_a_pot_opened_this_month_is_new_not_judged():
    pot = Pot(name="Fresh")
    doc = Document(
        pots=[pot],
        pot_entries=[PotEntry(pot_id=pot.id, on=date(2026, 11, 2), amount_pence=5_000)],
    )
    (health,) = calc.pot_health(doc, date(2026, 11, 5))
    assert (health.months_of_history, health.average_net, health.verdict) == (0, 0, "new")


def test_an_empty_pot_still_spending_says_so_rather_than_steady():
    pot = Pot(name="Spent")
    doc = Document(
        pots=[pot],
        pot_entries=[
            PotEntry(pot_id=pot.id, on=date(2026, 9, 1), amount_pence=40_000, kind="opening"),
            PotEntry(pot_id=pot.id, on=date(2026, 10, 1), amount_pence=-40_000, kind="spend"),
        ],
    )
    (health,) = calc.pot_health(doc, date(2026, 11, 5))
    assert (health.balance, health.verdict) == (0, "empty")


def test_pot_health_growing_steady_and_archived(doc: Document):
    holidays, car, _general = doc.pots
    car.archived = True
    doc.pot_entries.append(
        PotEntry(pot_id=holidays.id, on=date(2026, 10, 28), amount_pence=12_000, kind="payday")
    )
    health = {h.pot.name: h for h in calc.pot_health(doc, date(2026, 11, 5))}
    assert "Car" not in health
    assert health["Holidays"].verdict == "growing"
    assert health["Savings"].verdict == "steady"  # nothing moved, only imported
    assert health["Savings"].months_left is None


def test_payday_pots_excludes_the_general_pot(doc: Document):
    """The spreadsheet did the same, by summing B2:B9 rather than B2:B10."""
    names = [p.name for p in calc.payday_pots(doc)]
    assert names == ["Holidays", "Car"]
    assert calc.pots_monthly_total(doc) == 50_000


def test_archived_pots_are_excluded(doc: Document):
    doc.pots[0].archived = True
    assert "Holidays" not in [p.name for p in calc.payday_pots(doc)]
    assert calc.pots_monthly_total(doc) == 5_000


def test_summary_excludes_inactive_lines(doc: Document):
    totals = calc.summary(doc)
    assert totals.income == 500_000  # the 50000 "Cancelled" line is inactive
    assert totals.outgoings == 100_000  # the 5000 "Old thing" line is inactive


def test_summary_splits_bills_savings_spends_and_debt(doc: Document):
    """Debt is the ticked lines plus the ticked pots' top-ups; savings the
    other pots' top-ups; spends everything else; an inactive line counts
    nowhere. Flags only, as a document from before `category` has them."""
    mortgage, _bills, old = doc.outgoings
    mortgage.debt = True
    old.debt = True  # inactive
    car = next(p for p in doc.pots if p.name == "Car")
    car.debt = True
    totals = calc.summary(doc)
    assert totals.bills == 10_000
    assert totals.debt == 90_000 + 5_000
    assert totals.savings == 45_000  # Holidays; the general pot isn't topped up
    # The bills account transfer, and the 240000 left over.
    assert totals.spends == 110_000 + 240_000
    assert totals.outgoings == 100_000
    assert totals.lines == {"bills": 10_000, "savings": 0, "spends": 0, "debt": 90_000}


def test_a_line_or_pot_counts_toward_the_purpose_it_is_marked(doc: Document):
    mortgage, bills, _old = doc.outgoings
    mortgage.set_purpose("savings")
    bills.set_purpose("spends")
    holidays = next(p for p in doc.pots if p.name == "Holidays")
    holidays.set_purpose("spends")
    car = next(p for p in doc.pots if p.name == "Car")
    car.set_purpose("bills")
    totals = calc.summary(doc)
    assert totals.bills == 5_000
    assert totals.savings == 90_000
    assert totals.debt == 0
    assert totals.spends == 10_000 + 45_000 + 110_000 + 240_000
    assert totals.lines == {"bills": 0, "savings": 90_000, "spends": 10_000, "debt": 0}


def test_spends_takes_the_overspend_too(doc: Document):
    """The four always fill income, so planning more than comes in shows as
    negative spends rather than a part that doesn't add up."""
    doc.outgoings[0].amount_pence = 400_000
    totals = calc.summary(doc)
    assert totals.spare < 0
    assert totals.spends == 110_000 + totals.spare


def test_without_a_pot_tracking_transfer_the_pots_are_not_in_the_plan(doc: Document):
    doc.transfers = [t for t in doc.transfers if not t.tracks_pots]
    totals = calc.summary(doc)
    assert (totals.savings, totals.debt) == (0, 0)
    assert totals.spends == 110_000 + totals.spare


def test_summary_spare_ignores_the_remainder_transfer(doc: Document):
    """Counting the remainder would subtract the same money twice.

    Transfers are: the pot-tracking one (50000), a fixed 110000, and the
    remainder. Spare is what the remainder step will actually move.
    """
    totals = calc.summary(doc)
    assert totals.savings_and_spends == 160_000
    assert totals.spare == 500_000 - 100_000 - 160_000


def test_pot_tracking_transfer_follows_the_pots(doc: Document):
    """The drift the spreadsheet had: two copies of the same number.

    Changing a pot's monthly amount must move the transfer with it, with
    nothing else edited.
    """
    tracking = next(t for t in doc.transfers if t.tracks_pots)
    assert calc.transfer_amount(doc, tracking.id) == 50_000

    doc.pots[0].monthly_pence = 60_000
    assert calc.transfer_amount(doc, tracking.id) == 65_000
    assert calc.summary(doc).savings_and_spends == 175_000


def test_transfer_amount_is_none_for_an_unknown_transfer(doc: Document):
    assert calc.transfer_amount(doc, "does-not-exist") is None


def test_days_until_and_rolling(doc: Document, today: date):
    mot = next(r for r in doc.renewals if r.kind == "Car MOT")
    sim = next(r for r in doc.renewals if r.kind == "SIM")
    assert calc.days_until(mot, today) == 47
    # A rolling contract has no expiry, so it is never due and never nags.
    assert calc.days_until(sim, today) is None
    assert calc.is_due(sim, today) is False


def test_is_due_respects_per_renewal_notice(doc: Document, today: date):
    mot = next(r for r in doc.renewals if r.kind == "Car MOT")
    assert calc.is_due(mot, today) is True  # 47 days, inside the default 60

    mot.notice_days = 30
    assert calc.is_due(mot, today) is False


def test_renewals_sort_with_rolling_last(doc: Document, today: date):
    order = [r.kind for r in calc.renewals_by_date(doc, today)]
    assert order == ["Car MOT", "Mortgage", "SIM"]


def test_wealth_totals_skip_accounts_with_no_figure(doc: Document):
    """The private pension has a current value and no known projection;
    Army and State are the other way round."""
    assert calc.wealth_total(doc) == 2_367_900
    assert calc.projection_total(doc) == 850_000 + 1_150_000


def test_retirement_estimate_total_is_the_lump_sum_counterpart(doc: Document, today: date):
    """`projection_total`'s annual-income figures never count towards this,
    and vice versa — see `projected_retirement_value` for why."""
    assert calc.retirement_estimate_total(doc, today) == 0  # no target year set yet

    pension = doc.wealth_accounts[0]
    pension.target_retirement_year = today.year + 10
    doc.wealth_snapshots.append(
        WealthSnapshot(
            account_id=pension.id, as_of=today, current_pence=3_000_000, year_growth=0.05
        )
    )
    expected = calc.projected_retirement_value(pension, doc.latest_snapshot(pension.id), today)
    assert calc.retirement_estimate_total(doc, today) == expected
    assert expected > 0


def test_projection_total_never_counts_an_estimated_pot_value(today: date):
    """A lump-sum pot estimate and an annual income figure are different
    kinds of quantity, so `projection_total` must never add them together —
    it stays strictly the known, defined-benefit-style figures."""
    known = WealthAccount(company="Known")
    estimated = WealthAccount(company="Estimated", target_retirement_year=today.year + 10)
    document = Document(wealth_accounts=[known, estimated])
    document.wealth_snapshots = [
        WealthSnapshot(
            account_id=known.id, as_of=today, current_pence=1_000, yearly_projection_pence=2_000
        ),
        WealthSnapshot(account_id=estimated.id, as_of=today, current_pence=1_000, year_growth=0.05),
    ]

    # The estimated account's own retirement value is computable...
    assert (
        calc.projected_retirement_value(estimated, document.latest_snapshot(estimated.id), today)
        is not None
    )
    # ...but must not show up in the total of known figures.
    assert calc.projection_total(document) == 2_000


def test_wealth_total_uses_only_the_newest_snapshot(doc: Document):
    pension = doc.wealth_accounts[0]
    before = calc.wealth_total(doc)
    doc.wealth_snapshots.append(
        WealthSnapshot(account_id=pension.id, as_of=date(2026, 9, 1), current_pence=3_000_000)
    )
    # Replaces the April figure rather than adding to it.
    assert calc.wealth_total(doc) == before - 2_367_900 + 3_000_000


def test_annualised_growth_scales_a_short_gap_up_to_a_year(today: date):
    """A ratio comparable across snapshots however far apart they land.

    Doubled in exactly half a year is a much faster rate than doubled over a
    full one, so the same 2x move must annualise to a bigger number. With
    only one prior reading to compare against, this is the same as the old
    immediately-previous behaviour.
    """
    six_months_ago = WealthSnapshot(account_id="a", as_of=date(2026, 3, 20), current_pence=1_000)
    a_year_ago = WealthSnapshot(account_id="a", as_of=date(2025, 9, 20), current_pence=1_000)

    fast = calc.annualised_growth([six_months_ago], 2_000, today)
    slow = calc.annualised_growth([a_year_ago], 2_000, today)
    assert fast is not None and slow is not None

    assert fast > slow
    assert slow == pytest.approx(1.0, abs=0.01)  # doubling in ~a year is ~100%


def test_annualised_growth_prefers_the_reading_closest_to_a_year_back(today: date):
    """A household with more than a year of quarterly snapshots should not
    have its rate driven by just the latest, noisiest quarter.

    The pot sat flat at 1,900 for nine months, then rose to 2,000 in the
    final quarter alone — a true ~5% move for the year. Measured against
    only the last quarter, that same rise gets raised to roughly the fourth
    power annualising it, and reads as a much bigger rate than the account
    actually grew by over the year.
    """
    a_year_ago = WealthSnapshot(account_id="a", as_of=date(2025, 9, 20), current_pence=1_900)
    last_quarter = WealthSnapshot(account_id="a", as_of=date(2026, 6, 25), current_pence=1_900)

    from_last_quarter_alone = calc.annualised_growth([last_quarter], 2_000, today)
    using_full_history = calc.annualised_growth([a_year_ago, last_quarter], 2_000, today)
    assert from_last_quarter_alone is not None and using_full_history is not None

    assert using_full_history == pytest.approx(0.0527, abs=0.005)
    assert from_last_quarter_alone > using_full_history


def test_annualised_growth_is_none_without_a_comparable_previous_figure(today: date):
    never_valued = WealthSnapshot(account_id="a", as_of=today, current_pence=None)
    was_worthless = WealthSnapshot(account_id="a", as_of=date(2025, 1, 1), current_pence=0)
    same_day = WealthSnapshot(account_id="a", as_of=today, current_pence=1_000)

    assert calc.annualised_growth([], 1_000, today) is None
    assert calc.annualised_growth([never_valued], 1_000, today) is None
    assert calc.annualised_growth([was_worthless], 1_000, today) is None
    assert calc.annualised_growth([same_day], 1_000, today) is None


def test_valuation_change_covers_the_full_tracked_history():
    """The change since the earliest reading, not just the latest step —
    the same span a sparkline of the history covers."""
    history = [
        WealthSnapshot(account_id="a", as_of=date(2025, 1, 1), current_pence=1_000),
        WealthSnapshot(account_id="a", as_of=date(2025, 6, 1), current_pence=900),  # a dip
        WealthSnapshot(account_id="a", as_of=date(2026, 1, 1), current_pence=1_200),
    ]
    change = calc.valuation_change(history)
    assert change is not None
    assert change.amount_pence == 200
    assert change.fraction == pytest.approx(0.2)
    assert change.since == date(2025, 1, 1)


def test_valuation_change_is_none_without_two_valued_readings():
    one_reading = [WealthSnapshot(account_id="a", as_of=date(2025, 1, 1), current_pence=1_000)]
    never_valued = [WealthSnapshot(account_id="a", as_of=date(2025, 1, 1), current_pence=None)]
    was_worthless = [
        WealthSnapshot(account_id="a", as_of=date(2025, 1, 1), current_pence=0),
        WealthSnapshot(account_id="a", as_of=date(2026, 1, 1), current_pence=500),
    ]

    assert calc.valuation_change([]) is None
    assert calc.valuation_change(one_reading) is None
    assert calc.valuation_change(never_valued) is None
    assert calc.valuation_change(was_worthless) is None


def test_projected_retirement_value_compounds_the_observed_growth_rate(today: date):
    """A defined-contribution pot's stand-in for a defined-benefit figure."""
    account = WealthAccount(company="A Fund", target_retirement_year=today.year + 10)
    latest = WealthSnapshot(
        account_id=account.id, as_of=today, current_pence=1_000_00, year_growth=0.05
    )

    projected = calc.projected_retirement_value(account, latest, today)

    assert projected == round(1_000_00 * 1.05**10)


@pytest.mark.parametrize("observed_growth", [0.63, -0.90])
def test_projected_retirement_value_clamps_an_extreme_growth_rate(today: date, observed_growth):
    """A few good months read as an annualised 63% must not become millions
    compounded across decades — see the module docstring on the cap."""
    account = WealthAccount(company="A Fund", target_retirement_year=today.year + 20)
    latest = WealthSnapshot(
        account_id=account.id, as_of=today, current_pence=1_000_00, year_growth=observed_growth
    )

    projected = calc.projected_retirement_value(account, latest, today)

    cap = settings().retirement_growth_cap
    capped_rate = cap if observed_growth > 0 else -cap
    assert projected == round(1_000_00 * (1 + capped_rate) ** 20)


def test_projected_retirement_value_prefers_an_assumed_rate_over_the_observed_one(today: date):
    """The observed rate blends market return with contributions landing on top
    of it; the override is how a household says "ignore that, assume X%"."""
    account = WealthAccount(
        company="A Fund", target_retirement_year=today.year + 10, assumed_growth_rate=0.04
    )
    latest = WealthSnapshot(
        account_id=account.id, as_of=today, current_pence=1_000_00, year_growth=0.09
    )

    projected = calc.projected_retirement_value(account, latest, today)

    assert projected == round(1_000_00 * 1.04**10)


def test_projected_retirement_value_uses_the_assumed_rate_without_any_observed_growth(
    today: date,
):
    """A first-ever valuation has no `year_growth` of its own, but an assumed
    rate needs no history to compound from."""
    account = WealthAccount(
        company="A Fund", target_retirement_year=today.year + 10, assumed_growth_rate=0.04
    )
    latest = WealthSnapshot(account_id=account.id, as_of=today, current_pence=1_000_00)

    projected = calc.projected_retirement_value(account, latest, today)

    assert projected == round(1_000_00 * 1.04**10)


def test_projected_retirement_value_clamps_an_extreme_assumed_rate(today: date):
    """The cap guards against a fat-fingered override the same way it guards
    against a noisy observed reading."""
    account = WealthAccount(
        company="A Fund", target_retirement_year=today.year + 20, assumed_growth_rate=0.63
    )
    latest = WealthSnapshot(account_id=account.id, as_of=today, current_pence=1_000_00)

    projected = calc.projected_retirement_value(account, latest, today)

    cap = settings().retirement_growth_cap
    assert projected == round(1_000_00 * (1 + cap) ** 20)


@pytest.mark.parametrize(
    ("target_year_offset", "current_pence", "year_growth"),
    [
        (None, 100_00, 0.05),  # no target year set
        (10, None, 0.05),  # never valued
        (10, 100_00, None),  # no growth rate to compound (e.g. first valuation)
        (0, 100_00, 0.05),  # target year already reached
    ],
)
def test_projected_retirement_value_is_none_without_enough_to_go_on(
    today: date, target_year_offset, current_pence, year_growth
):
    account = WealthAccount(
        company="A Fund",
        target_retirement_year=(
            today.year + target_year_offset if target_year_offset is not None else None
        ),
    )
    latest = WealthSnapshot(
        account_id=account.id, as_of=today, current_pence=current_pence, year_growth=year_growth
    )
    assert calc.projected_retirement_value(account, latest, today) is None


def test_projected_retirement_value_is_none_with_no_snapshot_at_all(today: date):
    account = WealthAccount(company="A Fund", target_retirement_year=today.year + 10)
    assert calc.projected_retirement_value(account, None, today) is None


def test_attention_flags_payday_renewal_and_stale_wealth(doc: Document):
    # On the 28th: on or after the household's payday, so the nag is live.
    state = calc.attention(doc, date(2026, 9, 28))
    titles = [item.title for item in state.items]

    assert any("Payday not yet run" in t for t in titles)
    assert "Car MOT" in titles
    assert any("out of date" in t for t in titles)
    # The overdrawn Car pot.
    assert "Car is overdrawn" in titles
    # The mortgage, still 253 days out.
    assert "Mortgage" not in titles
    assert state.quiet_renewals == 2


def test_attention_is_quiet_when_nothing_is_due(today: date):
    """An all-clear must be reachable, or the panel is decoration."""
    document = Document(
        pots=[Pot(name="Holidays", monthly_pence=1000)],
        renewals=[Renewal(kind="Far off", expires_on=date(2029, 1, 1))],
    )
    document.payday_runs.append(PaydayRun(month=calc.current_month(today), run_on=today))
    state = calc.attention(document, today)
    assert state.items == []
    assert state.quiet_renewals == 1


def test_payday_is_not_flagged_before_the_28th(doc: Document):
    """The run cannot happen before the 28th, so nagging earlier is just noise."""
    early = calc.attention(doc, date(2026, 9, 20))
    assert not any("Payday" in i.title for i in early.items)


def test_payday_urgency_depends_on_how_late_it_is(doc: Document):
    """Not urgent the day it lands, so the panel is not red the instant it can be."""
    on_time = calc.attention(doc, date(2026, 9, 28))
    late = calc.attention(doc, date(2026, 9, 30))
    assert next(i for i in on_time.items if "Payday" in i.title).urgent is False
    assert next(i for i in late.items if "Payday" in i.title).urgent is True


def test_attention_ignores_archived_overdrawn_pots(doc: Document, today: date):
    car = next(p for p in doc.pots if p.name == "Car")
    car.archived = True
    titles = [i.title for i in calc.attention(doc, today).items]
    assert "Car is overdrawn" not in titles


def test_overdue_renewal_names_the_company(today: date):
    """An expired renewal reads "<company> — expired N days ago", not just the date math."""
    document = Document(
        renewals=[
            Renewal(kind="Boiler service", company="A Gas Co", expires_on=date(2026, 9, 15)),
        ],
    )
    item = next(i for i in calc.attention(document, today).items if i.title == "Boiler service")
    assert item.detail == "A Gas Co — expired 5 days ago"


def test_no_payday_nag_when_there_are_no_pots(today: date):
    document = Document()
    assert calc.attention(document, today).items == []


def test_wealth_account_with_no_snapshot_is_not_stale(today: date):
    """Never valued is a different problem from out of date."""
    document = Document(wealth_accounts=[WealthAccount(company="New")])
    assert calc.oldest_wealth_snapshot(document) is None
    assert calc.attention(document, today).items == []


def test_pot_entries_are_newest_first(doc: Document):
    car = next(p for p in doc.pots if p.name == "Car")
    entries = doc.entries_for(car.id)
    assert [e.on for e in entries] == [date(2026, 9, 2), date(2026, 8, 31)]


def test_entries_for_ignores_other_pots(doc: Document):
    holidays = next(p for p in doc.pots if p.name == "Holidays")
    doc.pot_entries.append(PotEntry(pot_id="not-a-pot", on=date(2026, 9, 1), amount_pence=999_999))
    assert len(doc.entries_for(holidays.id)) == 1
    # And a stray entry cannot inflate a total.
    assert calc.pots_total(doc) == 127_500 - 10_000 + 630_000


def test_recent_movements_windows_and_collapses_payday(today: date):
    doc = Document()
    doc.pots = [Pot(id="a", name="Car"), Pot(id="b", name="Ted")]
    e = lambda pot, on, amt, kind="deposit", note="": PotEntry(  # noqa: E731
        pot_id=pot, on=on, amount_pence=amt, kind=kind, note=note
    )
    doc.pot_entries = [
        e("a", date(2026, 9, 14), -500, "spend", "old"),  # 6 days back: in
        e("a", date(2026, 9, 13), -900, "spend"),  # 7 days back: out
        e("a", date(2026, 9, 20), -1000, "spend", "tyres"),
        e("a", date(2026, 9, 18), 2500, "payday"),
        e("b", date(2026, 9, 18), 1000, "payday"),
        e("b", date(2026, 9, 19), 100, "opening"),
        e("b", date(2026, 9, 21), 100),  # future: out
    ]

    moves = calc.recent_movements(doc, today)

    assert [(m.label, m.amount_pence) for m in moves] == [
        ("Car · tyres", -1000),
        ("Payday · 2 pots", 3500),
        ("Car · old", -500),
    ]


def test_recent_movements_includes_valuations_and_empty_paydays(today: date):
    doc = Document()
    doc.wealth_accounts = [WealthAccount(id="w", company="Aviva")]
    snap = lambda on, cur: WealthSnapshot(account_id="w", as_of=on, current_pence=cur)  # noqa: E731
    doc.wealth_snapshots = [
        snap(date(2026, 6, 1), 100_000),
        snap(date(2026, 9, 19), 110_000),
        WealthSnapshot(account_id="w", as_of=date(2026, 9, 20), yearly_projection_pence=5),
    ]
    doc.payday_runs = [PaydayRun(month="2026-09", run_on=date(2026, 9, 18))]

    moves = calc.recent_movements(doc, today)

    assert [(m.label, m.amount_pence) for m in moves] == [
        ("Aviva · valuation", 10_000),
        ("Payday · nothing moved", 0),
    ]


# --- properties -----------------------------------------------------------------
#
# Every figure here is derived, so the guarantee is that the derivations agree
# with each other and with the ledger for *any* document, not just `doc`.

AMOUNT = st.integers(min_value=-(10**9), max_value=10**9)
DAY = st.dates(min_value=date(2000, 1, 1), max_value=date(2100, 12, 31))
POT_IDS = ("p1", "p2", "p3", "p4")


@st.composite
def ledgers(draw: st.DrawFn) -> Document:
    """Pots, some archived, and entries against them, a few against no pot at all."""
    pots = [
        Pot(
            id=pot_id,
            name=pot_id,
            monthly_pence=draw(st.integers(min_value=0, max_value=10**7)),
            counts_toward_payday=draw(st.booleans()),
            archived=draw(st.booleans()),
            debt=draw(st.booleans()),
            bills=draw(st.booleans()),
        )
        for pot_id in POT_IDS[: draw(st.integers(min_value=0, max_value=len(POT_IDS)))]
    ]
    entries = draw(
        st.lists(
            st.builds(
                PotEntry,
                pot_id=st.sampled_from([*POT_IDS, "deleted"]),
                on=DAY,
                amount_pence=AMOUNT,
                kind=st.sampled_from(["payday", "deposit", "spend", "opening", "adjustment"]),
            ),
            max_size=40,
        )
    )
    return Document(pots=pots, pot_entries=entries)


@given(ledgers())
def test_every_balance_is_its_ledger_and_the_total_is_the_live_pots(doc: Document):
    balances = calc.pot_balances(doc)
    assert set(balances) == {p.id for p in doc.pots}
    for pot in doc.pots:
        assert balances[pot.id] == calc.pot_balance(doc, pot.id)
    live = {p.id for p in doc.pots if not p.archived}
    assert calc.pots_total(doc) == sum(e.amount_pence for e in doc.pot_entries if e.pot_id in live)


@given(ledgers())
def test_ledger_months_account_for_every_entry_once(doc: Document):
    """Grouping loses nothing, and the newest month ends on every pot's balance."""
    months = calc.pots_ledger_months(doc)
    balances = calc.pot_balances(doc)
    grouped = [e.id for m in months for e in m.entries]
    assert sorted(grouped) == sorted(e.id for e in doc.pot_entries if e.pot_id in balances)
    assert [m.month for m in months] == sorted({m.month for m in months}, reverse=True)
    for m in months:
        assert all(e.on.replace(day=1) == m.month for e in m.entries)
        assert m.paid_in >= 0 >= m.paid_out
        assert m.net + m.imported + m.adjusted == sum(e.amount_pence for e in m.entries)
    assert (months[0].closing if months else 0) == sum(balances.values())


@given(st.integers(-(10**12), 10**12), st.integers(1, 120))
def test_average_is_within_half_a_penny(total: int, count: int):
    assert abs(calc.average(total, count) * count - total) * 2 <= count


@given(ledgers(), DAY)
def test_monthly_flows_end_on_every_pots_balance(doc: Document, today: date):
    """Nothing after `today` is counted, and every month adds up."""
    known = {p.id for p in doc.pots}
    doc.pot_entries = [e for e in doc.pot_entries if e.on <= today]
    flows = calc.monthly_flows(doc, today)
    assert len(flows) == 12
    assert flows[-1].closing == sum(e.amount_pence for e in doc.pot_entries if e.pot_id in known)
    for before, after in pairwise(flows):
        assert after.paid_in >= 0 >= after.paid_out
        moved_or_not = after.closing - before.closing
        others = sum(
            e.amount_pence
            for e in doc.pot_entries
            if e.pot_id in known
            and e.on.replace(day=1) == after.month
            and e.kind in ("opening", "adjustment")
        )
        assert moved_or_not == after.net + others


def incomes_active(doc: Document) -> list[int]:
    return [line.amount_pence for line in doc.income if line.active]


@given(ledgers(), st.lists(AMOUNT, max_size=10), st.lists(AMOUNT, max_size=10), st.data())
def test_spare_is_always_what_is_left(
    doc: Document, incomes: list[int], outgoings: list[int], data: st.DataObject
):
    """income - outgoings - committed == spare, and the pots transfer tracks the pots."""
    doc.income = [Line(name="in", amount_pence=a, active=data.draw(st.booleans())) for a in incomes]
    # A purpose, or none and the old flag, as a document from before it has.
    doc.outgoings = [
        Line(
            name="out",
            amount_pence=a,
            category=data.draw(st.sampled_from([None, *PURPOSES])),
            debt=data.draw(st.booleans()),
        )
        for a in outgoings
    ]
    for pot in doc.pots:
        pot.category = data.draw(st.sampled_from([None, *PURPOSES]))
        pot.debt, pot.bills = data.draw(st.booleans()), data.draw(st.booleans())
    fixed = data.draw(AMOUNT)
    doc.transfers = [
        Transfer(label="pots", amount_pence=data.draw(AMOUNT), tracks_pots=True),
        Transfer(label="fixed", amount_pence=fixed),
        Transfer(label="the rest", amount_pence=None),
    ]
    result = calc.summary(doc)

    assert result.income == sum(incomes_active(doc))
    assert result.outgoings == sum(outgoings)
    # Worked out from the pots here, not with calc's own helpers, so a
    # regression in which pots a payday tops up shows up as a wrong total.
    payday = sum(p.monthly_pence for p in doc.pots if p.counts_toward_payday and not p.archived)
    assert result.savings_and_spends == payday + fixed
    assert result.spare == sum(incomes_active(doc)) - sum(outgoings) - payday - fixed
    # Split by purpose, the same money: every penny of income lands in
    # exactly one part, spends taking what is left.
    assert result.bills + result.savings + result.spends + result.debt == result.income
    for purpose in PURPOSES:
        lines = sum(x.amount_pence for x in doc.outgoings if x.purpose == purpose)
        pots = sum(
            p.monthly_pence
            for p in doc.pots
            if p.counts_toward_payday and not p.archived and p.purpose == purpose
        )
        extra = fixed + result.spare if purpose == "spends" else 0
        assert getattr(result, purpose) == lines + pots + extra, purpose
    # Each breakdown adds up to its figure on the insights page.
    for part in calc.PLAN_PARTS:
        listed = sum(i.amount_pence for i in calc.plan_breakdown(doc, part))
        assert listed == getattr(result, part), part


# Both found by this test: a 7x jump in a day overflowed, and a negative
# valuation came back as a complex number. Either failed the snapshot's save.
@example(previous=1, current=7, previous_on=date(2000, 1, 1), gap=1)
@example(previous=1, current=-1, previous_on=date(2000, 1, 1), gap=1)
@given(
    previous=st.integers(min_value=1, max_value=10**12),
    current=st.integers(min_value=-(10**12), max_value=10**12),
    previous_on=DAY,
    gap=st.integers(min_value=-30, max_value=3000),
)
def test_annualised_growth_is_a_real_number_or_none(
    previous: int, current: int, previous_on: date, gap: int
):
    """It's stored on the snapshot, so anything but a finite float or None is a 500."""
    history = [WealthSnapshot(account_id="a", as_of=previous_on, current_pence=previous)]
    growth = calc.annualised_growth(history, current, previous_on + timedelta(days=gap))
    if gap <= 0:
        assert growth is None
    else:
        assert growth is None or (isinstance(growth, float) and math.isfinite(growth))


@given(
    value=st.integers(min_value=1, max_value=10**12),
    previous_on=DAY,
    gap=st.integers(min_value=1, max_value=3000),
)
def test_no_change_is_no_growth(value: int, previous_on: date, gap: int):
    history = [WealthSnapshot(account_id="a", as_of=previous_on, current_pence=value)]
    growth = calc.annualised_growth(history, value, previous_on + timedelta(days=gap))
    assert growth == pytest.approx(0.0, abs=1e-12)


@given(
    current=st.integers(min_value=0, max_value=10**11),
    rate=st.floats(allow_nan=False, allow_infinity=False, min_value=-10, max_value=10),
    years_ahead=st.integers(min_value=1, max_value=60),
)
def test_a_retirement_estimate_never_compounds_past_the_cap(
    current: int, rate: float, years_ahead: int
):
    """One noisy rate compounded over decades is the bug the cap exists for."""
    today = date(2026, 11, 17)
    account = WealthAccount(
        company="x", target_retirement_year=today.year + years_ahead, assumed_growth_rate=rate
    )
    latest = WealthSnapshot(account_id=account.id, as_of=today, current_pence=current)
    estimate = calc.projected_retirement_value(account, latest, today)

    cap = settings().retirement_growth_cap
    assert estimate is not None
    assert round(current * (1 - cap) ** years_ahead) <= estimate
    assert estimate <= round(current * (1 + cap) ** years_ahead)


def test_plan_breakdown_refuses_an_unknown_part(doc: Document):
    with pytest.raises(ValueError, match="no such part"):
        calc.plan_breakdown(doc, "lottery")


def test_a_bills_pot_moves_its_top_up_from_savings_to_bills(doc: Document):
    car = next(p for p in doc.pots if p.name == "Car")
    car.bills = True
    totals = calc.summary(doc)
    assert (totals.bills, totals.savings) == (100_000 + 5_000, 45_000)
    assert [i.label for i in calc.plan_breakdown(doc, "bills")] == ["Mortgage", "Bills", "Car"]


def test_the_spends_breakdown_lists_the_other_transfers_and_what_is_left(doc: Document):
    doc.outgoings[1].set_purpose("spends")
    items = calc.plan_breakdown(doc, "spends")
    assert [(i.label, i.amount_pence) for i in items] == [
        (calc.LEFT_OVER, 240_000),
        ("Bills account", 110_000),
        ("Bills", 10_000),
    ]


def test_an_overspend_is_named_in_the_spends_breakdown(doc: Document):
    doc.outgoings[0].amount_pence = 400_000
    items = calc.plan_breakdown(doc, "spends")
    assert (calc.OVERSPENT, calc.summary(doc).spare) in [(i.label, i.amount_pence) for i in items]
    assert calc.LEFT_OVER not in [i.label for i in items]


def test_a_purpose_wins_over_the_old_flags_and_keeps_them_in_step():
    """The flags are what a release from before `category` reads back."""
    line = Line(name="x", debt=True, category="spends")
    assert line.purpose == "spends"
    line.set_purpose("debt")
    assert (line.category, line.debt) == ("debt", True)
    line.set_purpose("savings")
    assert (line.category, line.debt) == ("savings", False)
    pot = Pot(name="x", debt=True)
    pot.set_purpose("bills")
    assert (pot.purpose, pot.debt, pot.bills) == ("bills", False, True)
    pot.set_purpose("spends")
    assert (pot.purpose, pot.debt, pot.bills) == ("spends", False, False)


def test_a_pot_with_both_flags_counts_as_debt():
    """The form sets one at most, but a stored document could carry both."""
    assert Pot(name="x", debt=True, bills=True).purpose == "debt"
