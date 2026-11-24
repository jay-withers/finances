"""A sample document, for working on the app with no Azure and no spreadsheet.

Invented figures that are deliberately *not* the household's: working on a
layout should not mean having real balances on screen in a screen-share, and a
sample that looks plausible but wrong is safer than one that looks right.

It is shaped to put every screen into a state worth looking at — a pot that is
overdrawn, a renewal inside its notice window, a wealth figure old enough to be
stale, and a month whose payday has not been run.
"""

from __future__ import annotations

from datetime import date, timedelta

from .model import (
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


def _months_back(today: date, count: int) -> date:
    """The last day of the month `count` months before `today`'s."""
    following = today.year * 12 + today.month - count  # the month after, counted from year 0
    year, month = divmod(following, 12)
    return date(year, month + 1, 1) - timedelta(days=1)


def sample_document(today: date | None = None) -> Document:
    """Build the sample. Dated relative to `today` so it never goes stale."""
    today = today or date.today()
    doc = Document()

    doc.income = [
        Line(name="Salary", amount_pence=320_000),
        Line(name="Second salary", amount_pence=185_000),
    ]
    doc.outgoings = [
        # Two debts, so insights has debt to show apart from spending.
        Line(name="Mortgage", amount_pence=88_000, debt=True),
        Line(name="Credit card", amount_pence=15_000, debt=True),
        Line(name="Council Tax", amount_pence=19_500),
        Line(name="Energy", amount_pence=14_200),
        Line(name="Broadband", amount_pence=3_900),
        Line(name="Car insurance", amount_pence=5_600),
        Line(name="Streaming", amount_pence=1_599),
        # An inactive line, so the dimmed row and the "not counted" behaviour
        # are both visible without editing anything.
        Line(name="Old gym membership", amount_pence=2_499, active=False),
    ]

    pots = [
        Pot(name="Holidays", monthly_pence=30_000),
        Pot(name="Christmas", monthly_pence=7_500),
        Pot(name="Car", monthly_pence=10_000),
        Pot(name="House repairs", monthly_pence=12_000),
        # A pot saving up to clear a loan: its top-up counts as debt.
        Pot(name="Car loan", monthly_pence=8_000, debt=True),
        # Put by for a bill paid in full each month: bills, not savings.
        Pot(name="Card bill", monthly_pence=25_000, bills=True),
        # No monthly contribution: the general pot, excluded from payday.
        Pot(name="Savings", monthly_pence=0, counts_toward_payday=False),
    ]
    doc.pots = pots

    # A year of history, so the ledger's months and the insights charts have
    # something to show: opening balances a year back, eleven paydays since,
    # and the spends a household's pots exist for. This month's payday is left
    # unrun, so the payday screen has its button and the attention panel nags.
    balances = [62_500, 4_000, 45_000, 18_000, 0, 25_000, 540_000]
    opened = _months_back(today, 12)
    for pot, balance in zip(pots, balances, strict=True):
        doc.pot_entries.append(
            PotEntry(
                pot_id=pot.id,
                on=opened,
                amount_pence=balance,
                kind="opening",
                note="Sample data",
            )
        )
    for back in range(11, 0, -1):
        payday = _months_back(today, back).replace(day=28)
        entries = [
            PotEntry(
                pot_id=p.id,
                on=payday,
                amount_pence=p.monthly_pence,
                kind="payday",
                note=f"Payday {payday:%Y-%m}",
            )
            for p in pots
            if p.counts_toward_payday
        ]
        doc.pot_entries += entries
        doc.payday_runs.append(
            PaydayRun(
                month=f"{payday.year:04d}-{payday.month:02d}",
                run_on=payday,
                entry_ids=[e.id for e in entries],
            )
        )
    spends = [
        (0, 9, -210_000, "Summer holiday"),  # Holidays
        (0, 4, -48_000, "Weekend away"),
        (1, 1, -82_000, "Presents"),  # Christmas, whenever it last fell
        (2, 6, -64_000, "Service and tyres"),  # Car
        (3, 7, -95_000, "Roof repair"),  # House repairs
        (6, 5, -150_000, "New sofa"),  # Savings
    ]
    for pot_index, back, amount, note in spends:
        doc.pot_entries.append(
            PotEntry(
                pot_id=pots[pot_index].id,
                on=_months_back(today, back).replace(day=12),
                amount_pence=amount,
                kind="spend",
                note=note,
            )
        )
    # And one this month — the one that leaves "House repairs" overdrawn.
    doc.pot_entries.append(
        PotEntry(
            pot_id=pots[3].id,
            # Nine days back, but never into last month.
            on=max(today.replace(day=1), today - timedelta(days=9)),
            amount_pence=-120_000,
            kind="spend",
            note="Boiler",
        )
    )

    monthly = sum(p.monthly_pence for p in pots if p.counts_toward_payday)
    doc.transfers = [
        Transfer(label="Savings account", amount_pence=monthly, tracks_pots=True),
        Transfer(label="Bills account", amount_pence=133_000),
        Transfer(label="Current account", amount_pence=None, note="whatever is left"),
    ]

    doc.renewals = [
        # Inside the default 60-day window, so the attention panel is populated.
        Renewal(
            kind="Car insurance", company="Example Insurance", expires_on=today + timedelta(days=23)
        ),
        Renewal(
            kind="Broadband", company="Example Telecom", expires_on=today + timedelta(days=140)
        ),
        Renewal(
            kind="Boiler service", company="Example Gas", expires_on=today + timedelta(days=300)
        ),
        Renewal(kind="Mobile", company="Example Mobile", rolling=True),
    ]

    # A private, defined-contribution pension — has a current pot value and a
    # target retirement year to estimate one for, no known figure for what it
    # pays in retirement — beside the two fixed, defined-benefit ones every
    # household has exactly one of: no current value, but their annual income
    # is known outright, which is what makes them uneditable (see
    # `WealthAccount.editable`) rather than a normal account.
    pension = WealthAccount(
        company="Example Private Pension",
        still_contributing=True,
        target_retirement_year=today.year + 22,
    )
    army = WealthAccount(company="Example Army Pension", editable=False)
    state = WealthAccount(company="State Pension", editable=False)
    doc.wealth_accounts = [pension, army, state]
    doc.wealth_snapshots = [
        WealthSnapshot(
            account_id=pension.id, as_of=today - timedelta(days=200), current_pence=3_800_000
        ),
        # Recent enough not to be stale, so the trend and the estimate both
        # show without waiting a quarter.
        WealthSnapshot(
            account_id=pension.id,
            as_of=today - timedelta(days=40),
            current_pence=4_210_000,
            year_growth=0.081,
        ),
        # Deliberately older than the 90-day staleness threshold, so the
        # quarterly nudge shows up without having to wait a quarter.
        WealthSnapshot(
            account_id=army.id, as_of=today - timedelta(days=118), yearly_projection_pence=850_000
        ),
        WealthSnapshot(
            account_id=state.id,
            as_of=today - timedelta(days=118),
            yearly_projection_pence=1_150_000,
        ),
    ]

    # No PaydayRun for this month on purpose: the home screen should open
    # showing that this month's payday has not been run.
    return doc
