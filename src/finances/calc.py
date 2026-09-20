"""Everything derived. Nothing here is stored, so nothing here can disagree.

The spreadsheet kept its answers in cells, which is why it drifted: the monthly
pot contributions were typed into `Savings!B` *and* into the `Outgoings`
savings block, and by the time it was migrated the two had diverged (Ted 50
against 25, Oven 55 against 52 — a £28 gap that quietly moved the "Spare" figure
by the same amount). Here the pot amounts are the single source and the
Savings/Spends total is computed from them.

Every function takes the document and returns a value. None of them mutate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .model import Document, Pot, Renewal, WealthAccount, WealthSnapshot
from .settings import settings


def current_month(today: date) -> str:
    """`"2026-09"` — the key a PaydayRun is recorded under."""
    return f"{today.year:04d}-{today.month:02d}"


# --- pots ---------------------------------------------------------------------


def pot_balance(doc: Document, pot_id: str) -> int:
    """What a pot is worth, as the sum of its ledger.

    Entries are signed, so a spend is a negative amount and this is a plain
    sum rather than a pair of totals subtracted.
    """
    return sum(e.amount_pence for e in doc.pot_entries if e.pot_id == pot_id)


def pot_balances(doc: Document) -> dict[str, int]:
    """Every pot's balance, keyed by id.

    One pass over the entries rather than one pass per pot: the difference is
    irrelevant at this size, but it means adding a pot cannot make the
    dashboard quadratic.
    """
    balances = {p.id: 0 for p in doc.pots}
    for entry in doc.pot_entries:
        if entry.pot_id in balances:
            balances[entry.pot_id] += entry.amount_pence
    return balances


def pots_total(doc: Document) -> int:
    """The combined balance of every live pot."""
    balances = pot_balances(doc)
    return sum(balances[p.id] for p in doc.pots if not p.archived)


def payday_pots(doc: Document) -> list[Pot]:
    """The pots a payday run tops up, in display order.

    Excludes the general savings pot, which has no monthly contribution — the
    spreadsheet excluded it from its own 925 total the same way, by summing
    `B2:B9` rather than `B2:B10`.
    """
    return [p for p in doc.pots if p.counts_toward_payday and not p.archived]


def pots_monthly_total(doc: Document) -> int:
    """What one payday run costs. The figure the spreadsheet had as 925."""
    return sum(p.monthly_pence for p in payday_pots(doc))


def household_payday(today: date) -> date:
    """The date both incomes have landed for `today`'s month.

    Jay is paid on the 28th. Sarah is paid on the 28th too, or the Friday
    before when the 28th falls on a weekend — so the 28th is always the
    later, or equal, of the two, and is what "payday" means for the
    household's own payday run.
    """
    return date(today.year, today.month, 28)


# --- the monthly picture ------------------------------------------------------


@dataclass(frozen=True)
class Summary:
    """The `Outgoings` sheet, recomputed."""

    income: int
    outgoings: int
    savings_and_spends: int
    spare: int


def transfer_amount(doc: Document, transfer_id: str) -> int | None:
    """What one payday transfer moves, or None for the remainder step.

    The transfer flagged `tracks_pots` reports the pots' monthly total rather
    than a stored figure, which is what stops the two drifting apart again.
    """
    transfer = next((t for t in doc.transfers if t.id == transfer_id), None)
    if transfer is None:
        return None
    if transfer.tracks_pots:
        return pots_monthly_total(doc)
    return transfer.amount_pence


def summary(doc: Document) -> Summary:
    """Money in, money out, what is committed, and what is left.

    `savings_and_spends` is every transfer with an amount — the remainder step
    is by definition what is left, so counting it would subtract the same money
    twice and always produce a spare of zero.
    """
    income = sum(line.amount_pence for line in doc.income if line.active)
    outgoings = sum(line.amount_pence for line in doc.outgoings if line.active)

    committed = 0
    for transfer in doc.transfers:
        amount = transfer_amount(doc, transfer.id)
        if amount is not None:
            committed += amount

    return Summary(
        income=income,
        outgoings=outgoings,
        savings_and_spends=committed,
        spare=income - outgoings - committed,
    )


# --- renewals -----------------------------------------------------------------


def days_until(renewal: Renewal, today: date) -> int | None:
    """Days until a renewal expires. None when it never does.

    A rolling contract — the two Lebara SIMs — has no expiry, so it is never
    due and never nags. The spreadsheet had to give those a fake 2001 date
    because it had nowhere to say so.
    """
    if renewal.rolling or renewal.expires_on is None:
        return None
    return (renewal.expires_on - today).days


def notice_days(renewal: Renewal) -> int:
    """How far ahead this renewal wants warning."""
    if renewal.notice_days is not None:
        return renewal.notice_days
    return settings().default_notice_days


def is_due(renewal: Renewal, today: date) -> bool:
    """Whether a renewal is inside its notice window — including overdue."""
    days = days_until(renewal, today)
    return days is not None and days <= notice_days(renewal)


def renewals_by_date(doc: Document, today: date) -> list[Renewal]:
    """Every renewal, soonest first, with rolling ones last.

    Rolling contracts sort to the end rather than being hidden: they are still
    worth seeing on the renewals page, just never worth being nagged about.
    """
    return sorted(
        doc.renewals,
        key=lambda r: (r.rolling or r.expires_on is None, r.expires_on or date.max, r.kind),
    )


def due_renewals(doc: Document, today: date) -> list[Renewal]:
    return [r for r in renewals_by_date(doc, today) if is_due(r, today)]


# --- wealth -------------------------------------------------------------------


def wealth_total(doc: Document) -> int:
    """The household's current valuation: the newest snapshot of each account.

    Accounts with no snapshot contribute nothing rather than breaking the sum —
    the Army and State pensions have a projection but no current value, which
    is exactly the spreadsheet's own `C6 = SUM(C2:C5)` over two blank cells.
    """
    total = 0
    for account in doc.wealth_accounts:
        snapshot = doc.latest_snapshot(account.id)
        if snapshot and snapshot.current_pence is not None:
            total += snapshot.current_pence
    return total


def projection_total(doc: Document) -> int:
    """Combined yearly projection across every account's newest snapshot."""
    total = 0
    for account in doc.wealth_accounts:
        snapshot = doc.latest_snapshot(account.id)
        if snapshot and snapshot.yearly_projection_pence is not None:
            total += snapshot.yearly_projection_pence
    return total


def oldest_wealth_snapshot(doc: Document) -> tuple[WealthAccount, WealthSnapshot] | None:
    """The account whose newest figure is the most out of date.

    An account with no snapshot at all is not "stale" — it has never been
    valued, which is a different problem and is visible on the wealth page as a
    blank row.
    """
    dated = []
    for account in doc.wealth_accounts:
        snapshot = doc.latest_snapshot(account.id)
        if snapshot is not None:
            dated.append((account, snapshot))
    if not dated:
        return None
    return min(dated, key=lambda pair: pair[1].as_of)


# --- what needs attention -----------------------------------------------------


@dataclass(frozen=True)
class Item:
    """One thing on the dashboard's attention panel.

    `urgent` drives colour only. Everything here is worth reading; some of it
    is worth reading first.
    """

    title: str
    detail: str = ""
    href: str = "/"
    urgent: bool = False


@dataclass(frozen=True)
class Attention:
    items: list[Item] = field(default_factory=list)
    # Renewals that exist but are not yet inside their notice window. Shown as
    # a count rather than a list, so the panel says what it is *not* worrying
    # about — an empty panel is otherwise indistinguishable from a broken one.
    quiet_renewals: int = 0


def attention(doc: Document, today: date) -> Attention:
    """Everything the two calendar reminders used to be responsible for.

    Ordered by how soon it matters: payday first because it is the one action
    with a deadline the household actually feels, then renewals by date, then
    the quarterly wealth nudge, then anything structurally wrong.
    """
    items: list[Item] = []

    month = current_month(today)
    payday_date = household_payday(today)
    if doc.pots and doc.payday_run_for(month) is None and today >= payday_date:
        items.append(
            Item(
                title=f"Payday not yet run for {today:%B %Y}",
                detail=f"{len(payday_pots(doc))} pots to top up",
                href="/payday",
                # A day's grace once it lands before this turns red — the run
                # cannot happen before the 28th, so nagging any earlier, or
                # the instant it arrives, is noise rather than a deadline.
                urgent=(today - payday_date).days >= 1,
            )
        )

    for renewal in due_renewals(doc, today):
        days = days_until(renewal, today)
        assert days is not None  # is_due() is false for a renewal with no date
        if days < 0:
            detail = f"expired {-days} days ago"
        elif days == 0:
            detail = "expires today"
        else:
            detail = f"in {days} days"
        if renewal.company:
            detail = f"{renewal.company} — {detail}"
        items.append(Item(title=renewal.kind, detail=detail, href="/renewals", urgent=days <= 14))

    oldest = oldest_wealth_snapshot(doc)
    if oldest is not None:
        account, snapshot = oldest
        age = (today - snapshot.as_of).days
        if age > settings().wealth_stale_days:
            items.append(
                Item(
                    title="Wealth figures are out of date",
                    detail=f"{account.company} last updated {snapshot.as_of:%-d %b %Y}"
                    f" — {age} days ago",
                    href="/wealth",
                )
            )

    balances = pot_balances(doc)
    for pot in doc.pots:
        if not pot.archived and balances[pot.id] < 0:
            items.append(
                Item(
                    title=f"{pot.name} is overdrawn",
                    detail=f"balance {balances[pot.id] / 100:,.2f}",
                    href=f"/pots/{pot.id}",
                    urgent=True,
                )
            )

    quiet = len([r for r in doc.renewals if not is_due(r, today)])
    return Attention(items=items, quiet_renewals=quiet)
