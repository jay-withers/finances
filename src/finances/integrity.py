"""The rules a finances document must keep, checked wherever it is read or written.

A document that parses can still be wrong in ways that move money on screen:
an entry whose pot is gone still counts toward nothing, a payday run that names
deleted entries is undone wrongly on a re-run, a spend stored positive grows a
pot instead of shrinking it. Every rule here is one the routes already keep;
`check` is what notices when one of them didn't.

It runs in three places, through jay-withers/appkit's `appkit.integrity`:

- `store.save` refuses to write a document that breaks a rule, so a route bug
  can't put one in storage.
- `selfcheck` runs it, with appkit's round trip, against the live document
  after every deploy. A release whose model would drop a stored value, or that
  reads the document as broken, is rolled back before it writes.
- the nightly backup runs it after the copy is safe in R2, so a document that
  went bad some other way fires the job-failure alert.

**A problem names a list and an id, never an amount, a name or a note.** These
lines reach Log Analytics. Ids are random hex (`model.new_id`). Not an index
either: a message must read the same before and after an unrelated change, which
deleting an earlier entry would shift, because `store.update` refuses only the
problems a change adds (see there).

Zero amounts are allowed in what is stored: a zero entry moves no balance, and
older deposits may hold one. The routes refuse new ones.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable

from .model import Document

__all__ = ["check", "dump", "parse"]

# A PaydayRun's month, as calc.current_month writes it.
_MONTH = re.compile(r"\d{4}-(0[1-9]|1[0-2])")


def parse(raw: bytes) -> Document:
    """The plain parse appkit round-trips. Never the backup's checking wrapper."""
    return Document.from_json(raw.decode("utf-8"))


def dump(document: Document) -> str:
    return document.to_json()


def check(doc: Document) -> list[str]:
    """One line per broken rule; empty when the document is consistent."""
    problems: list[str] = []

    for name, ids in (
        ("income", (x.id for x in doc.income)),
        ("outgoings", (x.id for x in doc.outgoings)),
        ("transfers", (x.id for x in doc.transfers)),
        ("pots", (x.id for x in doc.pots)),
        ("pot_entries", (x.id for x in doc.pot_entries)),
        ("payday_runs", (x.id for x in doc.payday_runs)),
        ("renewals", (x.id for x in doc.renewals)),
        ("wealth_accounts", (x.id for x in doc.wealth_accounts)),
        ("wealth_snapshots", (x.id for x in doc.wealth_snapshots)),
    ):
        problems += _duplicates(name, ids)

    pots = {p.id for p in doc.pots}
    for entry in doc.pot_entries:
        where = f"pot_entries (id {entry.id})"
        if entry.pot_id not in pots:
            problems.append(f"{where}: points at no pot")
        if entry.kind == "spend" and entry.amount_pence > 0:
            problems.append(f"{where}: a spend stored as money in")
        if entry.kind in ("deposit", "payday") and entry.amount_pence < 0:
            problems.append(f"{where}: a {entry.kind} stored as money out")

    entries = {e.id for e in doc.pot_entries}
    months: Counter[str] = Counter()
    for run in doc.payday_runs:
        where = f"payday_runs (id {run.id})"
        if not _MONTH.fullmatch(run.month):
            problems.append(f"{where}: month is not YYYY-MM")
        else:
            months[run.month] += 1
        missing = [e for e in run.entry_ids if e not in entries]
        if missing:
            problems.append(f"{where}: names {len(missing)} entry id(s) that no longer exist")
    for month, count in sorted(months.items()):
        if count > 1:
            problems.append(f"payday_runs: {count} runs for one month ({month})")

    accounts = {a.id for a in doc.wealth_accounts}
    for snapshot in doc.wealth_snapshots:
        if snapshot.account_id not in accounts:
            problems.append(f"wealth_snapshots (id {snapshot.id}): points at no account")

    return problems


def _duplicates(name: str, ids: Iterable[str]) -> list[str]:
    counts = Counter(ids)
    return [f"{name}: id {i} is used {n} times" for i, n in sorted(counts.items()) if n > 1]
