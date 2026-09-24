"""The document: everything the household tracks, in one JSON object.

Shaped around what the spreadsheet actually did, with one deliberate change.
The `Savings` sheet held a grid of month-end *balances* that were retyped each
month, so it recorded what a pot was worth and never why it moved. Here a pot
owns a ledger and its balance is derived — nothing stores a balance, so nothing
can disagree about one.

**Every amount is integer pence.** See money.py for why.

Ids are short random strings rather than sequential integers, because two
browser tabs writing concurrently would otherwise both pick the same next
integer and the ETag retry in store.py would silently merge them into one id
used twice.
"""

from __future__ import annotations

import json
import secrets
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

EntryKind = Literal["payday", "deposit", "spend", "opening", "adjustment"]

# The monthly transfer whose amount is "whatever is left" rather than a figure.
# Modelled as None rather than 0 so that a genuine £0 transfer stays possible.
REMAINDER = None


def new_id() -> str:
    """A short, collision-proof id. 8 hex characters is 4 bytes of entropy."""
    return secrets.token_hex(4)


class Line(BaseModel):
    """One recurring money-in or money-out commitment.

    `active` rather than deletion, so a cancelled subscription stops counting
    toward the monthly total without erasing the fact that it existed.
    """

    id: str = Field(default_factory=new_id)
    name: str
    amount_pence: int = 0
    active: bool = True
    note: str = ""


class Transfer(BaseModel):
    """One step of the payday transfer routine — the old `Monthly` sheet.

    `amount_pence = None` means "the remainder", which is how the last step
    works: everything not already moved stays where the salary landed.
    """

    id: str = Field(default_factory=new_id)
    label: str
    amount_pence: int | None = None
    note: str = ""
    # Set on the step that moves the savings total, so it tracks the pots
    # instead of being a figure someone has to remember to retype. This is the
    # drift the spreadsheet had: `Outgoings!G:H` said 897 while `Savings!B`
    # said 925.
    tracks_pots: bool = False


class Pot(BaseModel):
    """A savings pot, topped up monthly and spent down as needed."""

    id: str = Field(default_factory=new_id)
    name: str
    monthly_pence: int = 0
    # The general `Savings` pot has no monthly contribution and was excluded
    # from the spreadsheet's own 925 total, so it is excluded here too.
    counts_toward_payday: bool = True
    archived: bool = False


class PotEntry(BaseModel):
    """One movement in or out of a pot. Signed: spends are negative."""

    id: str = Field(default_factory=new_id)
    pot_id: str
    on: date
    amount_pence: int
    kind: EntryKind = "deposit"
    note: str = ""


class PaydayRun(BaseModel):
    """A record that payday was processed for a given month.

    Exists so the dashboard can say "not yet run for September" — which the
    spreadsheet could only express as an empty column that looked identical to
    a month that genuinely had nothing in it.
    """

    id: str = Field(default_factory=new_id)
    month: str  # YYYY-MM
    run_on: date
    entry_ids: list[str] = Field(default_factory=list)


class Renewal(BaseModel):
    """Something with an expiry date worth being reminded about.

    `rolling` covers the two Lebara SIMs, which the spreadsheet gave an
    obviously-fake 2001 date because it had nowhere to say "monthly, no end
    date". A rolling renewal is never due and never nags.
    """

    id: str = Field(default_factory=new_id)
    kind: str
    company: str = ""
    expires_on: date | None = None
    rolling: bool = False
    # None means "use settings().default_notice_days". A mortgage wants longer
    # notice than a SIM.
    notice_days: int | None = None
    comment: str = ""


class WealthAccount(BaseModel):
    """A pension or investment, valued periodically rather than continuously."""

    id: str = Field(default_factory=new_id)
    company: str
    planned_pot_pence: int | None = None
    notes: str = ""
    # Most of a household's pensions are frozen former-employer pots; this is
    # what lets the wealth page put the one still growing by contribution
    # first, not just by whichever happened to be updated most recently.
    still_contributing: bool = False
    # A defined-benefit pension (Army, State) states its retirement figure
    # outright; a defined-contribution one does not, so `calc.py` estimates it
    # by compounding this account's latest valuation forward to this year —
    # see `calc.projected_retirement_value`.
    target_retirement_year: int | None = None


class WealthSnapshot(BaseModel):
    """One valuation of one account, on one date.

    A list of these rather than a single current figure per account: the whole
    point of the quarterly ritual is the trend, and the spreadsheet threw away
    every previous reading.
    """

    id: str = Field(default_factory=new_id)
    account_id: str
    as_of: date
    current_pence: int | None = None
    yearly_projection_pence: int | None = None
    # A ratio, not a percentage: 0.2903 is 29.03%. Computed by
    # `calc.annualised_growth` against the account's previous snapshot when
    # this one is recorded — see there for what makes it None.
    year_growth: float | None = None


class Document(BaseModel):
    """Everything, as held in the blob.

    `schema_version` is written but not yet branched on. It exists so that the
    first incompatible change has somewhere to look, rather than having to infer
    the shape from which keys are present.
    """

    schema_version: int = 1

    income: list[Line] = Field(default_factory=list)
    outgoings: list[Line] = Field(default_factory=list)
    transfers: list[Transfer] = Field(default_factory=list)

    pots: list[Pot] = Field(default_factory=list)
    pot_entries: list[PotEntry] = Field(default_factory=list)
    payday_runs: list[PaydayRun] = Field(default_factory=list)

    renewals: list[Renewal] = Field(default_factory=list)

    wealth_accounts: list[WealthAccount] = Field(default_factory=list)
    wealth_snapshots: list[WealthSnapshot] = Field(default_factory=list)

    # --- lookups --------------------------------------------------------------
    #
    # Linear scans on purpose. These lists are tens of items, not thousands, and
    # an index would be another thing to keep consistent across a mutation.

    def pot(self, pot_id: str) -> Pot | None:
        return next((p for p in self.pots if p.id == pot_id), None)

    def entries_for(self, pot_id: str) -> list[PotEntry]:
        """A pot's ledger, newest first — the order it is read in."""
        entries = [e for e in self.pot_entries if e.pot_id == pot_id]
        return sorted(entries, key=lambda e: (e.on, e.id), reverse=True)

    def account(self, account_id: str) -> WealthAccount | None:
        return next((a for a in self.wealth_accounts if a.id == account_id), None)

    def snapshots_for(self, account_id: str) -> list[WealthSnapshot]:
        """An account's valuations, newest first."""
        snapshots = [s for s in self.wealth_snapshots if s.account_id == account_id]
        return sorted(snapshots, key=lambda s: (s.as_of, s.id), reverse=True)

    def latest_snapshot(self, account_id: str) -> WealthSnapshot | None:
        return next(iter(self.snapshots_for(account_id)), None)

    def payday_run_for(self, month: str) -> PaydayRun | None:
        """The run for a `YYYY-MM` month, if there was one."""
        return next((r for r in self.payday_runs if r.month == month), None)

    # --- serialisation --------------------------------------------------------

    def to_json(self) -> str:
        """The document as stored. Indented because it is read by people too:
        `finances show` pipes it to a terminal, and blob versioning diffs are
        only useful line by line."""
        return self.model_dump_json(indent=2)

    @classmethod
    def from_json(cls, raw: str) -> Document:
        return cls.model_validate(json.loads(raw))
