"""The derived values that replace the spreadsheet's formulas."""

from __future__ import annotations

from datetime import date

import pytest

from finances import calc
from finances.model import (
    Document,
    PaydayRun,
    Pot,
    PotEntry,
    Renewal,
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
    """Two accounts, one of which has no projection — as in the spreadsheet."""
    assert calc.wealth_total(doc) == 2_367_900 + 5_255_600
    assert calc.projection_total(doc) == 25_500_000


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
    full one, so the same 2x move must annualise to a bigger number.
    """
    six_months_ago = WealthSnapshot(account_id="a", as_of=date(2026, 3, 20), current_pence=1_000)
    a_year_ago = WealthSnapshot(account_id="a", as_of=date(2025, 9, 20), current_pence=1_000)

    fast = calc.annualised_growth(six_months_ago, 2_000, today)
    slow = calc.annualised_growth(a_year_ago, 2_000, today)

    assert fast > slow
    assert slow == pytest.approx(1.0, abs=0.01)  # doubling in ~a year is ~100%


def test_annualised_growth_is_none_without_a_comparable_previous_figure(today: date):
    never_valued = WealthSnapshot(account_id="a", as_of=today, current_pence=None)
    was_worthless = WealthSnapshot(account_id="a", as_of=date(2025, 1, 1), current_pence=0)
    same_day = WealthSnapshot(account_id="a", as_of=today, current_pence=1_000)

    assert calc.annualised_growth(None, 1_000, today) is None
    assert calc.annualised_growth(never_valued, 1_000, today) is None
    assert calc.annualised_growth(was_worthless, 1_000, today) is None
    assert calc.annualised_growth(same_day, 1_000, today) is None


def test_valuation_change_covers_the_full_tracked_history():
    """The change since the earliest reading, not just the latest step —
    the same span a sparkline of the history covers."""
    history = [
        WealthSnapshot(account_id="a", as_of=date(2025, 1, 1), current_pence=1_000),
        WealthSnapshot(account_id="a", as_of=date(2025, 6, 1), current_pence=900),  # a dip
        WealthSnapshot(account_id="a", as_of=date(2026, 1, 1), current_pence=1_200),
    ]
    change = calc.valuation_change(history)
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
