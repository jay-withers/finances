"""The spreadsheet migration, against the real file.

This runs against `Finances_3.xlsx` in the repository root rather than a
fixture, because the point of these assertions is that the *actual* numbers
come across. They are the figures in the sheet as of the migration; if the file
is ever updated, these change with it.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from finances import calc
from finances.importer import import_workbook
from finances.money import parse_money

WORKBOOK = Path(__file__).resolve().parent.parent / "Finances_3.xlsx"
AS_OF = date(2026, 9, 20)

pytestmark = pytest.mark.skipif(
    not WORKBOOK.exists(),
    reason="the spreadsheet is removed once the import has been verified",
)


@pytest.fixture(scope="module")
def result():
    return import_workbook(WORKBOOK, today=AS_OF)


def test_every_check_reconciles(result):
    """The guard that matters: no total may silently differ from the sheet."""
    failures = [c for c in result.checks if not c.ok]
    assert failures == [], [f"{c.label}: {c.sheet} vs {c.imported}" for c in failures]


def test_income_and_outgoings(result):
    doc = result.document
    assert [line.name for line in doc.income] == ["Jay", "Sarah"]
    assert calc.summary(doc).income == parse_money("5950")
    assert calc.summary(doc).outgoings == parse_money("2722.53")


def test_volleyball_is_kept_as_an_inactive_line(result):
    """A commitment with no amount is not the same as no commitment."""
    line = next(line for line in result.document.outgoings if line.name == "Volleyball")
    assert line.active is False
    assert line.amount_pence == 0


def test_pots_and_their_monthly_amounts(result):
    doc = result.document
    amounts = {p.name: p.monthly_pence for p in doc.pots}
    assert amounts["Holidays"] == parse_money("450")
    # The two the Outgoings block had wrong — Savings!B is the source.
    assert amounts["Ted"] == parse_money("50")
    assert amounts["Oven"] == parse_money("55")
    assert calc.pots_monthly_total(doc) == parse_money("925")


def test_general_savings_pot_is_excluded_from_payday(result):
    general = next(p for p in result.document.pots if p.name == "Savings")
    assert general.monthly_pence == 0
    assert general.counts_toward_payday is False


def test_pot_balances_come_from_august(result):
    doc = result.document
    balances = calc.pot_balances(doc)
    by_name = {p.name: balances[p.id] for p in doc.pots}
    assert by_name["Holidays"] == parse_money("1275")
    assert by_name["Jay Credit"] == parse_money("3480")
    assert calc.pots_total(doc) == parse_money("13935")


def test_opening_entries_carry_the_earlier_months_as_a_note(result):
    """History the sheet held as balances is kept as text, not invented as movements."""
    doc = result.document
    holidays = next(p for p in doc.pots if p.name == "Holidays")
    entry = doc.entries_for(holidays.id)[0]
    assert entry.kind == "opening"
    assert entry.on == date(2026, 8, 31)
    assert "Jan £2,700" in entry.note
    assert "Jul £2,425" in entry.note


def test_the_savings_block_drift_is_reported_not_copied(result):
    """The £28 gap between Outgoings!G:H and Savings!B."""
    doc = result.document
    assert calc.summary(doc).savings_and_spends == parse_money("3225")
    check = next(c for c in result.checks if c.label == "Savings / spends")
    assert "EXPECTED DIFFERENCE" in check.note
    assert "£28" in check.note


def test_spare_follows_from_the_corrected_figure(result):
    """£2.47, not the sheet's £30.47 — which was wrong by the same £28."""
    assert calc.summary(result.document).spare == parse_money("2.47")


def test_transfers(result):
    doc = result.document
    labels = [t.label for t in doc.transfers]
    assert labels == ["Savings to Chase", "First Direct", "Chase", "Lloyds"]

    tracking = doc.transfers[0]
    assert tracking.tracks_pots is True
    # Stored as None, derived as the pots' total: one source, not two.
    assert tracking.amount_pence is None
    assert calc.transfer_amount(doc, tracking.id) == parse_money("925")

    assert doc.transfers[1].amount_pence == parse_money("1100")
    assert doc.transfers[3].amount_pence is None  # the remainder


def test_renewals_and_placeholder_dates(result):
    doc = result.document
    assert len(doc.renewals) == 19

    mot = next(r for r in doc.renewals if r.kind == "Car MOT")
    assert mot.expires_on == date(2026, 11, 6)
    # "N/A" is not a company.
    assert mot.company == ""

    for kind in ("Ollie SIM", "Sarah SIM"):
        sim = next(r for r in doc.renewals if r.kind == kind)
        assert sim.rolling is True
        assert sim.expires_on is None

    card = next(r for r in doc.renewals if r.kind == "Jay Credit Card")
    assert card.comment == "£5101 as of 24/08/26"


def test_wealth(result):
    doc = result.document
    assert [a.company for a in doc.wealth_accounts] == ["Fidelity (RBC)", "Aviva", "Army", "State"]
    fidelity = doc.wealth_accounts[0]
    snapshot = doc.latest_snapshot(fidelity.id)
    assert snapshot.as_of == date(2026, 4, 29)
    assert snapshot.current_pence == parse_money("23679")
    assert snapshot.year_growth == pytest.approx(0.2903)

    assert calc.wealth_total(doc) == parse_money("76235")
    assert calc.projection_total(doc) == parse_money("276816")


def test_what_was_skipped_is_named(result):
    joined = " | ".join(result.skipped)
    assert "Volleyball" in joined
    assert "I42" in joined
    assert "savings/spends block" in joined


def test_the_stray_cell_is_not_imported(result):
    """Savings!I42 holds 2875 under no row label."""
    assert parse_money("2875") not in [e.amount_pence for e in result.document.pot_entries]


def test_import_is_deterministic_apart_from_ids(result):
    """Two runs of the same sheet must produce the same figures."""
    again = import_workbook(WORKBOOK, today=AS_OF)
    assert calc.summary(again.document) == calc.summary(result.document)
    assert calc.pots_total(again.document) == calc.pots_total(result.document)
