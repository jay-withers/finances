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
from datetime import date, timedelta

from .model import (
    PURPOSES,
    Document,
    Pot,
    PotEntry,
    Purpose,
    Renewal,
    WealthAccount,
    WealthSnapshot,
)
from .money import format_money
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
    home screen quadratic.
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


@dataclass(frozen=True)
class LedgerMonth:
    """One calendar month of a ledger, with its totals.

    Imports (`opening`) and corrections (`adjustment`) are kept apart from
    `paid_in`/`paid_out`: neither is money that moved that month, and counting
    the import would make its month look like the household saved thousands.
    `closing` is the running balance at month end, everything included, so the
    newest month's closing is the balance today.
    """

    month: date  # the 1st of the month
    entries: list[PotEntry]  # newest first
    paid_in: int
    paid_out: int  # zero or negative
    imported: int
    adjusted: int
    closing: int

    @property
    def net(self) -> int:
        """What moved that month: `paid_in + paid_out`."""
        return self.paid_in + self.paid_out


def ledger_months(entries: list[PotEntry]) -> list[LedgerMonth]:
    """`entries` grouped by calendar month, newest month first.

    Months with nothing recorded are left out rather than listed empty: a pot
    that sat untouched for a year would otherwise be twelve blank headings.
    """
    by_month: dict[date, list[PotEntry]] = {}
    for entry in sorted(entries, key=lambda e: (e.on, e.id)):
        by_month.setdefault(entry.on.replace(day=1), []).append(entry)

    months: list[LedgerMonth] = []
    balance = 0
    for month, items in sorted(by_month.items()):
        moved = [e.amount_pence for e in items if e.kind not in ("opening", "adjustment")]
        balance += sum(e.amount_pence for e in items)
        months.append(
            LedgerMonth(
                month=month,
                entries=items[::-1],
                paid_in=sum(a for a in moved if a > 0),
                paid_out=sum(a for a in moved if a < 0),
                imported=sum(e.amount_pence for e in items if e.kind == "opening"),
                adjusted=sum(e.amount_pence for e in items if e.kind == "adjustment"),
                closing=balance,
            )
        )
    return months[::-1]


def pots_ledger_months(doc: Document) -> list[LedgerMonth]:
    """Every pot's ledger, merged and grouped by month.

    Archived pots stay in: archiving is about what happens next, and taking
    their entries out would rewrite the totals of months they were live in.
    So the newest closing is every pot's balance, which is `pots_total` plus
    whatever an archived pot still holds (normally nothing). Entries for a pot
    that no longer exists are left out, as `pot_balances` leaves them out.
    """
    known = {p.id for p in doc.pots}
    return ledger_months([e for e in doc.pot_entries if e.pot_id in known])


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
class Movement:
    """One line of the recent-activity list: a single entry, or a whole payday.

    A payday run writes one entry per pot on the same date; listing them
    separately would bury everything else, so they collapse into one line.
    """

    on: date
    label: str
    amount_pence: int
    href: str


def recent_movements(doc: Document, today: date, days: int = 7) -> list[Movement]:
    """Money moved over the last `days` days, newest first: pot entries, payday
    runs, and pension valuations (as the change since the previous one).

    Goes by the entry's own `on` date, not when it was typed in, so a backdated
    entry lands where it belongs. `opening` entries are imports, not movement.
    """
    since = today - timedelta(days=days - 1)
    names = {p.id: p.name for p in doc.pots}
    moves: list[Movement] = []
    paydays: dict[date, list[PotEntry]] = {}

    for entry in doc.pot_entries:
        if entry.kind == "opening" or not since <= entry.on <= today:
            continue
        if entry.kind == "payday":
            paydays.setdefault(entry.on, []).append(entry)
            continue
        pot = names.get(entry.pot_id, "Deleted pot")
        label = f"{pot} \u00b7 {entry.note}" if entry.note else pot
        moves.append(Movement(entry.on, label, entry.amount_pence, f"/pots/{entry.pot_id}"))

    for on, entries in paydays.items():
        count = len(entries)
        label = f"Payday \u00b7 {count} pot{'' if count == 1 else 's'}"
        moves.append(Movement(on, label, sum(e.amount_pence for e in entries), "/payday"))

    # A run where every pot sat the month out leaves no entries to collapse, but
    # it still happened, and the Today page's "not yet run" nag clears because of it.
    for run in doc.payday_runs:
        if not run.entry_ids and since <= run.run_on <= today:
            moves.append(Movement(run.run_on, "Payday \u00b7 nothing moved", 0, "/payday"))

    for account in doc.wealth_accounts:
        history = doc.snapshots_for(account.id)  # newest first
        for i, snap in enumerate(history):
            if snap.current_pence is None or not since <= snap.as_of <= today:
                continue
            earlier_pence = next(
                (s.current_pence for s in history[i + 1 :] if s.current_pence is not None), None
            )
            if earlier_pence is None:
                label = f"{account.company} \u00b7 first valuation"
                moves.append(Movement(snap.as_of, label, snap.current_pence, "/wealth"))
            else:
                label = f"{account.company} \u00b7 valuation"
                change = snap.current_pence - earlier_pence
                moves.append(Movement(snap.as_of, label, change, "/wealth"))

    return sorted(moves, key=lambda m: m.on, reverse=True)


@dataclass(frozen=True)
class Summary:
    """The `Outgoings` sheet, recomputed."""

    income: int
    outgoings: int
    savings_and_spends: int
    spare: int
    # The active money-out lines' total by purpose, every purpose present,
    # for In / Out's footer. `bills` and the rest below add the pots.
    lines: dict[Purpose, int]
    # The same income again, by what it is for. Each is the money-out lines
    # with that purpose plus the payday top-ups of the pots with it. `spends`
    # also takes everything else: the payday transfers besides the pots (the
    # First Direct one) and whatever is left over. So the four always sum to
    # `income`, and `spends` is negative when the plan spends more than that.
    bills: int
    savings: int
    spends: int
    debt: int


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
    active_out = [line for line in doc.outgoings if line.active]
    outgoings = sum(line.amount_pence for line in active_out)
    lines = {p: sum(x.amount_pence for x in active_out if x.purpose == p) for p in PURPOSES}

    # The pots reach the plan only through a transfer that tracks them, so
    # their top-ups count once per such transfer.
    tracking = sum(1 for t in doc.transfers if t.tracks_pots)
    pots = {
        p: sum(x.monthly_pence for x in payday_pots(doc) if x.purpose == p) * tracking
        for p in PURPOSES
    }

    committed = 0
    for transfer in doc.transfers:
        amount = transfer_amount(doc, transfer.id)
        if amount is not None:
            committed += amount
    spare = income - outgoings - committed
    # What the payday transfers move besides the pots.
    others = committed - sum(pots.values())

    return Summary(
        income=income,
        outgoings=outgoings,
        savings_and_spends=committed,
        spare=spare,
        lines=lines,
        bills=lines["bills"] + pots["bills"],
        savings=lines["savings"] + pots["savings"],
        spends=lines["spends"] + pots["spends"] + others + spare,
        debt=lines["debt"] + pots["debt"],
    )


@dataclass(frozen=True)
class PlanItem:
    """One line, pot or transfer in a part of the monthly plan."""

    label: str
    amount_pence: int
    href: str  # where it is edited


PLAN_PARTS: tuple[Purpose, ...] = PURPOSES

# The item standing for what is left over, in the spends breakdown, and for
# what the plan spends beyond income when nothing is.
LEFT_OVER = "Left over"
OVERSPENT = "Planned beyond income"


def plan_breakdown(doc: Document, part: str) -> list[PlanItem]:
    """What makes up one of `summary`'s bills, savings, spends or debt,
    largest first. The items always sum to that figure.

    A pot counts once for each transfer that tracks the pots, as `summary`
    counts it; there is normally exactly one.
    """
    if part not in PLAN_PARTS:
        raise ValueError(f"no such part of the plan: {part}")
    tracking = sum(1 for t in doc.transfers if t.tracks_pots)
    items = [
        PlanItem(x.name, x.amount_pence, f"/monthly/{x.id}")
        for x in doc.outgoings
        if x.active and x.purpose == part
    ]
    items += [
        PlanItem(p.name, p.monthly_pence * tracking, f"/pots/{p.id}")
        for p in payday_pots(doc)
        if p.purpose == part
    ]
    if part == "spends":
        items += [
            PlanItem(t.label, t.amount_pence, "/payday")
            for t in doc.transfers
            if not t.tracks_pots and t.amount_pence is not None
        ]
        spare = summary(doc).spare
        items.append(PlanItem(LEFT_OVER if spare >= 0 else OVERSPENT, spare, "/monthly"))
    return sorted((i for i in items if i.amount_pence), key=lambda i: (-i.amount_pence, i.label))


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


def next_renewal(doc: Document, today: date) -> tuple[Renewal, int] | None:
    """The soonest renewal still ahead (today counts), with its days to go.

    Rolling ones never renew, and ones already expired are the attention
    panel's job, not this.
    """
    for renewal in renewals_by_date(doc, today):
        days = days_until(renewal, today)
        if days is not None and days >= 0:
            return renewal, days
    return None


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
    """Combined *known* yearly projection — a defined-benefit scheme's own
    stated annual income in retirement (Army, State), never a defined-
    contribution pot's estimated future value (`projected_retirement_value`),
    which is a different kind of figure and deliberately never counted here.
    """
    total = 0
    for account in doc.wealth_accounts:
        snapshot = doc.latest_snapshot(account.id)
        if snapshot and snapshot.yearly_projection_pence is not None:
            total += snapshot.yearly_projection_pence
    return total


@dataclass(frozen=True)
class ValuationChange:
    """How much an account has moved between its earliest and its most
    recent valuation — the same span a sparkline of its history covers."""

    amount_pence: int
    fraction: float
    since: date


def valued_readings(snapshots: list[WealthSnapshot]) -> list[tuple[date, int]]:
    """(as_of, current_pence) for the snapshots that have a current figure, oldest first.

    `snapshots_for` includes rows recorded for the projection alone, with no
    current figure; anything plotting or comparing valuations skips those.
    """
    return sorted(
        ((s.as_of, s.current_pence) for s in snapshots if s.current_pence is not None),
        key=lambda pair: pair[0],
    )


def valuation_change(snapshots: list[WealthSnapshot]) -> ValuationChange | None:
    """The change over the full tracked history, not just the latest step.

    None with fewer than two valued readings — nothing to compare — or an
    earliest figure of zero or less, which a percentage change against is
    meaningless.
    """
    valued = valued_readings(snapshots)
    if len(valued) < 2:
        return None
    (since, first_pence), (_, last_pence) = valued[0], valued[-1]
    if first_pence <= 0:
        return None
    delta = last_pence - first_pence
    return ValuationChange(amount_pence=delta, fraction=delta / first_pence, since=since)


def annualised_growth(
    history: list[WealthSnapshot], current_pence: int, as_of: date
) -> float | None:
    """The growth a new valuation implies, annualised so readings on any
    schedule are comparable.

    Compares against the valued reading closest to a year before `as_of`,
    not simply whichever one came immediately before it. With quarterly
    snapshots, comparing against the last one means annualising a three-month
    move — raising a small, noisy ratio to the fourth power, which is exactly
    how a so-so quarter reads as a much bigger annual rate than the account
    actually grew by. A reading close to a full year back needs almost no
    annualising at all, so the result is close to what actually happened over
    that year rather than an extrapolation of a slice of it. This naturally
    degrades to the old immediately-previous behaviour for an account with no
    reading older than that yet.

    A ratio, not a percentage: 0.29 means 29%. None when there is nothing to
    compare against — no prior valued reading, or the closest one is dated
    the same day as `as_of` or later — and None when no rate means anything:
    a negative valuation (a fractional power of a negative ratio is a complex
    number), or a jump so big over so few days that annualising it overflows
    a float (a reading with a digit too many, the day after the last one).
    Either used to fail the snapshot it was being stored on, so the valuation
    couldn't be saved at all.
    """
    if current_pence < 0:
        return None
    valued = [
        (s.as_of, s.current_pence)
        for s in history
        if s.current_pence is not None and s.current_pence > 0 and s.as_of < as_of
    ]
    if not valued:
        return None
    target = as_of - timedelta(days=365)
    previous_on, previous_pence = min(valued, key=lambda pair: abs((pair[0] - target).days))
    days = (as_of - previous_on).days
    years = days / 365.25
    try:
        return (current_pence / previous_pence) ** (1 / years) - 1
    except OverflowError:
        return None


def projected_retirement_value(
    account: WealthAccount, latest: WealthSnapshot | None, today: date
) -> int | None:
    """A rough pot-value estimate for a defined-contribution pension at
    retirement — a different figure from `yearly_projection_pence`, which is
    an annual income a defined-benefit scheme (Army, State) states outright.
    The two are never added together for exactly that reason: one is a lump
    sum, the other a rate.

    Compounds the latest valuation forward to the account's target retirement
    year, at `account.assumed_growth_rate` when the household has set one, or
    otherwise the growth rate its own last reading implied. The observed rate
    is the household's fallback, not its preference: it is worked out from the
    raw change in the pot's value, which for a `still_contributing` account
    blends market return with new money going in, so `assumed_growth_rate`
    exists to let the household override that blend with a rate they actually
    believe. Either way it is clamped to `settings().retirement_growth_cap`
    first: one noisy figure — a few good months read as an annualised 25%+, or
    a fat-fingered override — compounded across decades otherwise turns a
    four-figure pot into a "rough estimate" in the millions, which is not
    rough, it's wrong.

    None without enough to go on: no target year set, no current figure, no
    rate to compound (no override, and the very first valuation never has a
    growth rate of its own), or a target year already reached.
    """
    if account.target_retirement_year is None:
        return None
    if latest is None or latest.current_pence is None:
        return None
    rate = (
        account.assumed_growth_rate
        if account.assumed_growth_rate is not None
        else latest.year_growth
    )
    if rate is None:
        return None
    years = account.target_retirement_year - today.year
    if years <= 0:
        return None
    cap = settings().retirement_growth_cap
    rate = max(-cap, min(cap, rate))
    return round(latest.current_pence * (1 + rate) ** years)


def retirement_estimate_total(doc: Document, today: date) -> int:
    """Combined `projected_retirement_value` across every account that has
    one — the lump-sum counterpart to `projection_total`'s annual-income
    figure, kept as its own separate total for exactly the reason
    `projected_retirement_value` explains: the two are different kinds of
    quantity and must never be added together.
    """
    total = 0
    for account in doc.wealth_accounts:
        estimate = projected_retirement_value(account, doc.latest_snapshot(account.id), today)
        if estimate is not None:
            total += estimate
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


def stale_wealth(doc: Document, today: date) -> tuple[WealthAccount, WealthSnapshot, int] | None:
    """The most out-of-date valuation and its age in days, when it is older
    than `wealth_stale_days`. The one rule the attention panel and the home
    screen's Pensions tile both read."""
    oldest = oldest_wealth_snapshot(doc)
    if oldest is None:
        return None
    account, snapshot = oldest
    age = (today - snapshot.as_of).days
    return (account, snapshot, age) if age > settings().wealth_stale_days else None


def plural(count: int, noun: str) -> str:
    """`"1 day"`, `"2 days"`. The attention panel and the home tiles share it,
    so the same renewal never reads "1 days" in one place and "1 day" in another."""
    return f"{count} {noun}{'' if count == 1 else 's'}"


# --- what needs attention -----------------------------------------------------


@dataclass(frozen=True)
class Item:
    """One thing on the Today page's attention panel.

    `urgent` drives colour only. Everything here is worth reading; some of it
    is worth reading first.
    """

    title: str
    detail: str = ""
    href: str = "/today"
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
    # The pots a run would top up, not every pot: with all of them archived or
    # excluded there is nothing to run, so nothing to nag about.
    if payday_pots(doc) and doc.payday_run_for(month) is None and today >= payday_date:
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
        # S101: type narrowing only; is_due() is false for a renewal with no date, so -O stripping
        # it changes nothing.
        assert days is not None  # noqa: S101
        if days < 0:
            detail = f"expired {plural(-days, 'day')} ago"
        elif days == 0:
            detail = "expires today"
        else:
            detail = f"in {plural(days, 'day')}"
        if renewal.company:
            detail = f"{renewal.company} — {detail}"
        items.append(Item(title=renewal.kind, detail=detail, href="/renewals", urgent=days <= 14))

    stale = stale_wealth(doc, today)
    if stale is not None:
        account, snapshot, age = stale
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
                    detail=f"balance {format_money(balances[pot.id])}",
                    href=f"/pots/{pot.id}",
                    urgent=True,
                )
            )

    quiet = len([r for r in doc.renewals if not is_due(r, today)])
    return Attention(items=items, quiet_renewals=quiet)


# --- insights -----------------------------------------------------------------
#
# What the insights page shows. All of it is read off the pot ledgers, with the
# same rules as the ledger's month view: imports and corrections are not money
# that moved, and archived pots stay in the history.


def month_starts(today: date, count: int) -> list[date]:
    """The 1st of each of the last `count` months, oldest first, ending with
    `today`'s month."""
    year, month = today.year, today.month
    starts: list[date] = []
    for _ in range(count):
        starts.append(date(year, month, 1))
        year, month = (year, month - 1) if month > 1 else (year - 1, 12)
    return starts[::-1]


@dataclass(frozen=True)
class MonthFlow:
    """One month of the insights charts. Empty months are zeros, not gaps."""

    month: date
    paid_in: int
    paid_out: int  # zero or negative
    closing: int  # every pot's balance at the month's end

    @property
    def net(self) -> int:
        return self.paid_in + self.paid_out


def monthly_flows(doc: Document, today: date, months: int = 12) -> list[MonthFlow]:
    """The last `months` months of every pot together, oldest first.

    Unlike `pots_ledger_months`, a month with nothing in it is listed with the
    balance carried forward: a chart needs every month, a ledger does not.
    """
    starts = month_starts(today, months)
    known = {p.id for p in doc.pots}
    by_month = {m.month: m for m in pots_ledger_months(doc)}
    balance = sum(e.amount_pence for e in doc.pot_entries if e.pot_id in known and e.on < starts[0])
    flows: list[MonthFlow] = []
    for start in starts:
        ledger = by_month.get(start)
        if ledger is not None:
            balance = ledger.closing
        flows.append(
            MonthFlow(
                month=start,
                paid_in=ledger.paid_in if ledger else 0,
                paid_out=ledger.paid_out if ledger else 0,
                closing=balance,
            )
        )
    return flows


def average(total: int, count: int) -> int:
    """`total / count` rounded to the nearest penny, half away from zero, in
    integers: money never goes through a float."""
    magnitude = (abs(total) * 2 + count) // (count * 2)
    return magnitude if total >= 0 else -magnitude


@dataclass(frozen=True)
class PotHealth:
    """How one pot is doing, averaged over the last full months."""

    pot: Pot
    balance: int
    average_in: int
    average_out: int  # zero or negative
    # Set when the pot is shrinking: whole months until it empties at the
    # average rate. None when it is growing, flat, or already empty.
    months_left: int | None
    # The full months the averages cover: the window, or fewer for a pot
    # younger than it. Zero for a pot whose first entry is this month.
    months_of_history: int = 0

    @property
    def average_net(self) -> int:
        return self.average_in + self.average_out

    @property
    def verdict(self) -> str:
        """`overdrawn`, `empty`, `running down`, `new`, `growing` or `steady`."""
        if self.balance < 0:
            return "overdrawn"
        if self.balance == 0 and self.average_net < 0:
            return "empty"
        if self.months_left is not None:
            return "running down"
        if self.months_of_history == 0:
            return "new"
        return "growing" if self.average_net > 0 else "steady"


def pot_health(doc: Document, today: date, months: int = 12) -> list[PotHealth]:
    """Every live pot's average month over the `months` full months before
    `today`'s, worst first: overdrawn, then soonest empty, then the rest.

    A year by default, because pot spending is lumpy (one car service, one
    holiday) and six months can miss the spend a pot is saving for entirely.
    The current month is left out, so a pot isn't judged on a half month, and
    a pot younger than the window is averaged over the months it has existed:
    two months of spending spread over twelve would say it lasts six times
    longer than it will.
    """
    current = date(today.year, today.month, 1)
    balances = pot_balances(doc)
    results: list[PotHealth] = []
    for pot in doc.pots:
        if pot.archived:
            continue
        entries = [e for e in doc.pot_entries if e.pot_id == pot.id]
        born = min((e.on for e in entries), default=today)
        age = (current.year - born.year) * 12 + current.month - born.month
        history = max(0, min(months, age))
        first = month_starts(today, history + 1)[0]
        moved = [
            e.amount_pence
            for e in entries
            if first <= e.on < current and e.kind not in ("opening", "adjustment")
        ]
        average_in = average(sum(a for a in moved if a > 0), history) if history else 0
        average_out = average(sum(a for a in moved if a < 0), history) if history else 0
        net, balance = average_in + average_out, balances[pot.id]
        months_left = -(balance // net) if net < 0 and balance > 0 else None
        results.append(PotHealth(pot, balance, average_in, average_out, months_left, history))

    def urgency(health: PotHealth) -> tuple[int, int, str]:
        if health.balance < 0:
            return (0, health.balance, health.pot.name)
        if health.verdict == "empty":
            return (0, 0, health.pot.name)
        if health.months_left is not None:
            return (1, health.months_left, health.pot.name)
        return (2, -health.balance, health.pot.name)

    return sorted(results, key=urgency)
