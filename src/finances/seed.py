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
    Pot,
    PotEntry,
    Renewal,
    Transfer,
    WealthAccount,
    WealthSnapshot,
)


def sample_document(today: date | None = None) -> Document:
    """Build the sample. Dated relative to `today` so it never goes stale."""
    today = today or date.today()
    doc = Document()

    doc.income = [
        Line(name="Salary", amount_pence=320_000),
        Line(name="Second salary", amount_pence=185_000),
    ]
    doc.outgoings = [
        Line(name="Mortgage", amount_pence=88_000),
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
        # No monthly contribution: the general pot, excluded from payday.
        Pot(name="Savings", monthly_pence=0, counts_toward_payday=False),
    ]
    doc.pots = pots

    balances = [142_500, 31_000, 68_000, -4_500, 610_000]
    opened = today.replace(day=1) - timedelta(days=1)
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
    # One spend, so a pot page has something other than its opening line — and
    # it is the one that leaves "House repairs" overdrawn.
    doc.pot_entries.append(
        PotEntry(
            pot_id=pots[3].id,
            on=today - timedelta(days=9),
            amount_pence=-16_500,
            kind="spend",
            note="Boiler part",
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

    pension = WealthAccount(company="Example Pension")
    isa = WealthAccount(company="Example ISA")
    doc.wealth_accounts = [pension, isa]
    doc.wealth_snapshots = [
        # Deliberately older than the 90-day staleness threshold, so the
        # quarterly nudge shows up without having to wait a quarter.
        WealthSnapshot(
            account_id=pension.id,
            as_of=today - timedelta(days=118),
            current_pence=4_210_000,
            yearly_projection_pence=1_850_000,
            year_growth=0.081,
        ),
        WealthSnapshot(
            account_id=isa.id,
            as_of=today - timedelta(days=40),
            current_pence=1_640_000,
            year_growth=0.052,
        ),
    ]

    # No PaydayRun on purpose: the dashboard should open showing that this
    # month's payday has not been run.
    return doc
