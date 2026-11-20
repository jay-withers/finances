"""integrity.py: the rules a stored document keeps, one broken document per rule.

`check` runs in `store.save`, the selfcheck and the backup, so a rule that is
wrong refuses a good write or rolls back a good release. Each rule has a test
that it fires and the fixtures, the seed and every route test show it doesn't
fire on real shapes.
"""

from __future__ import annotations

from datetime import date

import pytest

from finances import integrity, seed
from finances.model import Document, PaydayRun, PotEntry, WealthSnapshot


def test_the_fixture_document_keeps_every_rule(doc):
    assert integrity.check(doc) == []


def test_the_seed_sample_keeps_every_rule():
    assert integrity.check(seed.sample_document(date(2026, 9, 20))) == []


def test_an_empty_document_keeps_every_rule():
    assert integrity.check(Document()) == []


def test_an_entry_whose_pot_is_gone(doc):
    doc.pot_entries.append(
        PotEntry(id="0rphan00", pot_id="nopot000", on=date(2026, 9, 1), amount_pence=100)
    )
    assert integrity.check(doc) == ["pot_entries (id 0rphan00): points at no pot"]


def test_a_spend_stored_positive(doc):
    doc.pot_entries[2].amount_pence = 30_000
    entry = doc.pot_entries[2]
    assert integrity.check(doc) == [f"pot_entries (id {entry.id}): a spend stored as money in"]


@pytest.mark.parametrize("kind", ["deposit", "payday"])
def test_money_in_stored_negative(doc, kind):
    pot = doc.pots[0]
    doc.pot_entries.append(
        PotEntry(id="negative", pot_id=pot.id, on=date(2026, 9, 1), amount_pence=-1, kind=kind)
    )
    assert integrity.check(doc) == [f"pot_entries (id negative): a {kind} stored as money out"]


@pytest.mark.parametrize("kind", ["opening", "adjustment"])
def test_an_opening_or_adjustment_may_have_either_sign(doc, kind):
    doc.pot_entries.append(
        PotEntry(pot_id=doc.pots[0].id, on=date(2026, 9, 1), amount_pence=-5, kind=kind)
    )
    assert integrity.check(doc) == []


def test_a_zero_entry_is_allowed_in_storage(doc):
    """It moves no balance, and an old deposit may hold one; the routes refuse new ones."""
    doc.pot_entries.append(PotEntry(pot_id=doc.pots[0].id, on=date(2026, 9, 1), amount_pence=0))
    assert integrity.check(doc) == []


def test_a_payday_run_naming_a_deleted_entry(doc):
    doc.payday_runs.append(
        PaydayRun(id="run00001", month="2026-09", run_on=date(2026, 9, 25), entry_ids=["gone0000"])
    )
    assert integrity.check(doc) == [
        "payday_runs (id run00001): names 1 entry id(s) that no longer exist"
    ]


def test_two_payday_runs_for_one_month(doc):
    doc.payday_runs += [
        PaydayRun(month="2026-09", run_on=date(2026, 9, 25)),
        PaydayRun(month="2026-09", run_on=date(2026, 9, 26)),
    ]
    assert integrity.check(doc) == ["payday_runs: 2 runs for one month (2026-09)"]


@pytest.mark.parametrize("month", ["2026-9", "2026-13", "September", ""])
def test_a_payday_run_month_that_is_not_yyyy_mm(doc, month):
    doc.payday_runs.append(PaydayRun(id="run00001", month=month, run_on=date(2026, 9, 25)))
    assert integrity.check(doc) == ["payday_runs (id run00001): month is not YYYY-MM"]


def test_a_snapshot_whose_account_is_gone(doc):
    doc.wealth_snapshots.append(
        WealthSnapshot(id="snap0001", account_id="noacct00", as_of=date(2026, 9, 1))
    )
    assert integrity.check(doc) == ["wealth_snapshots (id snap0001): points at no account"]


def test_a_duplicate_id(doc):
    doc.pots[1].id = doc.pots[0].id
    found = integrity.check(doc)
    assert f"pots: id {doc.pots[0].id} is used 2 times" in found


def test_a_problem_names_ids_and_never_an_amount_or_a_name(doc):
    entry = doc.pot_entries[2]
    entry.id = "e0000001"
    entry.amount_pence = 30_000
    entry.note = "Garage bill"
    assert integrity.check(doc) == ["pot_entries (id e0000001): a spend stored as money in"]


def test_parse_and_dump_round_trip_the_stored_bytes(doc):
    raw = doc.to_json().encode("utf-8")
    assert integrity.parse(raw) == doc
    assert integrity.dump(integrity.parse(raw)) == doc.to_json()
