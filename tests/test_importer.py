"""The spreadsheet migration: against the real file, and a synthetic one.

The first half runs against `Finances_3.xlsx` in the repository root rather
than a fixture, because the point of those assertions is that the *actual*
numbers came across. They are the figures in the sheet as of the migration, and
they skip now that the file has been removed.

The second half builds a small workbook in the sheet's layout with openpyxl, so
every branch of the importer still runs on every PR (each shape of
row it has a rule for is in `_build_workbook`): plain values rather than
formulas, because `load_workbook(data_only=True)` reads a library-written
formula as None. Its figures are invented and round, chosen for the
assertions, like conftest's `doc`.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest
from openpyxl import Workbook
from openpyxl import load_workbook as open_for_edit  # not the importer's data_only read

from finances import calc
from finances.importer import Result, _as_date, import_workbook
from finances.money import parse_money

WORKBOOK = Path(__file__).resolve().parent.parent / "Finances_3.xlsx"
AS_OF = date(2026, 9, 20)


# On each real-file test rather than the module, so the synthetic workbook's
# tests below still run without the file. A new real-file test takes it too.
needs_workbook = pytest.mark.skipif(
    not WORKBOOK.exists(),
    reason="the spreadsheet is removed once the import has been verified",
)


@pytest.fixture(scope="module")
def result():
    return import_workbook(WORKBOOK, today=AS_OF)


@needs_workbook
def test_every_check_reconciles(result):
    """The guard that matters: no total may silently differ from the sheet."""
    failures = [c for c in result.checks if not c.ok]
    assert failures == [], [f"{c.label}: {c.sheet} vs {c.imported}" for c in failures]


@needs_workbook
def test_income_and_outgoings(result):
    doc = result.document
    assert [line.name for line in doc.income] == ["Jay", "Sarah"]
    assert calc.summary(doc).income == parse_money("5950")
    assert calc.summary(doc).outgoings == parse_money("2722.53")


@needs_workbook
def test_volleyball_is_kept_as_an_inactive_line(result):
    """A commitment with no amount is not the same as no commitment."""
    line = next(line for line in result.document.outgoings if line.name == "Volleyball")
    assert line.active is False
    assert line.amount_pence == 0


@needs_workbook
def test_pots_and_their_monthly_amounts(result):
    doc = result.document
    amounts = {p.name: p.monthly_pence for p in doc.pots}
    assert amounts["Holidays"] == parse_money("450")
    # The two the Outgoings block had wrong — Savings!B is the source.
    assert amounts["Ted"] == parse_money("50")
    assert amounts["Oven"] == parse_money("55")
    assert calc.pots_monthly_total(doc) == parse_money("925")


@needs_workbook
def test_general_savings_pot_is_excluded_from_payday(result):
    general = next(p for p in result.document.pots if p.name == "Savings")
    assert general.monthly_pence == 0
    assert general.counts_toward_payday is False


@needs_workbook
def test_pot_balances_come_from_august(result):
    doc = result.document
    balances = calc.pot_balances(doc)
    by_name = {p.name: balances[p.id] for p in doc.pots}
    assert by_name["Holidays"] == parse_money("1275")
    assert by_name["Jay Credit"] == parse_money("3480")
    assert calc.pots_total(doc) == parse_money("13935")


@needs_workbook
def test_opening_entries_carry_the_earlier_months_as_a_note(result):
    """History the sheet held as balances is kept as text, not invented as movements."""
    doc = result.document
    holidays = next(p for p in doc.pots if p.name == "Holidays")
    entry = doc.entries_for(holidays.id)[0]
    assert entry.kind == "opening"
    assert entry.on == date(2026, 8, 31)
    assert "Jan £2,700" in entry.note
    assert "Jul £2,425" in entry.note


@needs_workbook
def test_the_savings_block_drift_is_reported_not_copied(result):
    """The £28 gap between Outgoings!G:H and Savings!B."""
    doc = result.document
    assert calc.summary(doc).savings_and_spends == parse_money("3225")
    check = next(c for c in result.checks if c.label == "Savings / spends")
    assert "EXPECTED DIFFERENCE" in check.note
    assert "£28" in check.note


@needs_workbook
def test_spare_follows_from_the_corrected_figure(result):
    """£2.47, not the sheet's £30.47 — which was wrong by the same £28."""
    assert calc.summary(result.document).spare == parse_money("2.47")


@needs_workbook
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


@needs_workbook
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


@needs_workbook
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


@needs_workbook
def test_what_was_skipped_is_named(result):
    joined = " | ".join(result.skipped)
    assert "Volleyball" in joined
    assert "I42" in joined
    assert "savings/spends block" in joined


@needs_workbook
def test_the_stray_cell_is_not_imported(result):
    """Savings!I42 holds 2875 under no row label."""
    assert parse_money("2875") not in [e.amount_pence for e in result.document.pot_entries]


@needs_workbook
def test_import_is_deterministic_apart_from_ids(result):
    """Two runs of the same sheet must produce the same figures."""
    again = import_workbook(WORKBOOK, today=AS_OF)
    assert calc.summary(again.document) == calc.summary(result.document)
    assert calc.pots_total(again.document) == calc.pots_total(result.document)


# --- a synthetic workbook in the sheet's layout -------------------------------


def _write(sheet, cells: dict[str, object]) -> None:
    for ref, value in cells.items():
        sheet[ref] = value


def _build_workbook(
    path: Path, *, savings_months: bool = True, savings_total_row: bool = True
) -> Path:
    """Every sheet the importer reads, with each shape it has a branch for.

    `reconcile()` reads the real sheet's total cells by address (Outgoings!B4,
    E41, H13, H15; Savings!B11 and the month's row 11; Wealth!C6, E6), so the
    totals sit at those addresses whatever the rows above them hold.
    """
    book = Workbook()
    book.remove(book.active)

    outgoings = book.create_sheet("Outgoings")
    _write(
        outgoings,
        {
            "A2": "Jay",
            "B2": 3000,
            "A3": "Sarah",
            "B3": 2000,
            "A4": "Total",
            "B4": 5000,
            "D2": "Rent",
            "E2": 1200,
            "D3": "Volleyball",  # a name and no amount
            "D4": "Gym",
            "E4": 30,
            # D5 is blank with more lines under it, so it is stepped over.
            "D6": "Phone",
            "E6": 20,
            # A subtotal with no blank above it is skipped, not imported.
            "D7": "Total",
            "E7": 1250,
            # D8 is blank, as the real sheet's D40 is, and the total under
            # it ends the column.
            "D9": "Total",
            "E9": 1250,
            "E41": 1250,
            "H13": 1572,  # the drifted savings/spends block: £28 under
            "H15": 2198,
        },
    )

    savings = book.create_sheet("Savings")
    rows = {
        "A2": "Holidays",
        "B2": 450,
        "A3": "Ted",
        "B3": 50,
        "A4": "Savings",
        "B4": 0,  # the general pot: no monthly amount
        "B11": 500,
    }
    if savings_total_row:
        rows["A5"] = "Total"  # without it, the blank A5 ends the pots instead
    if savings_months:
        rows |= {
            "C2": 2700,
            "D2": 2900,
            "E2": 2425,
            "C3": 100,
            "E3": 150,  # no February figure
            "C4": 1000,
            "E4": 1000,
            "E11": 3575,
        }
    _write(savings, rows)

    monthly = book.create_sheet("Monthly")
    _write(
        monthly,
        {
            "A1": 1,
            "B1": 500,
            "C1": "Savings to Chase",
            "D1": "the pots",
            "A2": 2,
            "B2": 1100,
            "C2": "First Direct",
            "A3": 3,
            "B3": "Remainder",
            "C3": "Lloyds",
            "D3": "what is left",
        },
    )

    expiry = book.create_sheet("Expiry dates")
    _write(
        expiry,
        {
            "A1": "Kind",
            "B1": "Company",
            "C1": "Expires",
            "D1": "Comment",
            "A2": "Car MOT",
            "B2": "N/A",
            "C2": datetime(2026, 11, 6),
            "D2": "book early",
            "A3": "Ollie SIM",
            "B3": "Lebara",
            "C3": datetime(2001, 1, 1),
            "A4": "Boiler",
            "B4": "British Gas",
            "C4": 46000,  # an Excel serial
            "A5": "Gym",
            "B5": "PureGym",  # no date at all
        },
    )

    wealth = book.create_sheet("Wealth")
    _write(
        wealth,
        {
            "A2": "Fidelity",
            "B2": datetime(2026, 4, 29),
            "C2": 20000,
            "E2": 50000,
            "F2": 0.29,
            "G2": "workplace pension",
            "A3": "Old ISA",  # no as-of date: skipped, and said so
            "A4": "State",
            "B4": date(2026, 4, 6),
            "E4": 9000,
            "A5": "Total",
            "C6": 20000,
            "E6": 59000,
        },
    )

    book.save(path)
    return path


@pytest.fixture
def synthetic(tmp_path) -> Result:
    return import_workbook(_build_workbook(tmp_path / "book.xlsx"), today=AS_OF)


def test_synthetic_every_check_reconciles(synthetic):
    failures = [c for c in synthetic.checks if not c.ok]
    assert failures == [], [f"{c.label}: {c.sheet} vs {c.imported}" for c in failures]
    labels = [c.label for c in synthetic.checks]
    assert "Pot balances (March)" in labels


def test_synthetic_income_and_outgoings(synthetic):
    doc = synthetic.document
    assert [(line.name, line.amount_pence) for line in doc.income] == [
        ("Jay", parse_money("3000")),
        ("Sarah", parse_money("2000")),
    ]
    assert [line.name for line in doc.outgoings] == ["Rent", "Volleyball", "Gym", "Phone"]
    volleyball = doc.outgoings[1]
    assert (volleyball.active, volleyball.amount_pence, volleyball.note) == (
        False,
        0,
        "no amount",
    )
    totals = calc.summary(doc)
    assert totals.income == parse_money("5000")
    assert totals.outgoings == parse_money("1250")


def test_synthetic_pots_open_at_the_last_filled_month(synthetic):
    doc = synthetic.document
    assert [(p.name, p.monthly_pence, p.counts_toward_payday) for p in doc.pots] == [
        ("Holidays", parse_money("450"), True),
        ("Ted", parse_money("50"), True),
        ("Savings", 0, False),
    ]
    ted = next(p for p in doc.pots if p.name == "Ted")
    (entry,) = doc.entries_for(ted.id)
    assert entry.kind == "opening"
    assert entry.on == date(2026, 3, 31)
    assert entry.amount_pence == parse_money("150")
    # The missing February is left out of the history, not written as £0.
    assert entry.note.endswith("Jan £100, Mar £150")
    assert calc.pots_total(doc) == parse_money("3575")


def test_synthetic_a_sheet_with_no_month_filled_imports_no_balances(tmp_path):
    # Also ends the pots on a blank row rather than a Total one.
    path = _build_workbook(tmp_path / "book.xlsx", savings_months=False, savings_total_row=False)
    result = import_workbook(path, today=AS_OF)
    assert [p.name for p in result.document.pots] == ["Holidays", "Ted", "Savings"]
    assert result.document.pot_entries == []
    assert not any(c.label.startswith("Pot balances") for c in result.checks)


def test_synthetic_transfers(synthetic):
    doc = synthetic.document
    first, second, last = doc.transfers
    assert (first.label, first.tracks_pots, first.amount_pence, first.note) == (
        "Savings to Chase",
        True,
        None,
        "the pots",
    )
    assert calc.transfer_amount(doc, first.id) == parse_money("500")
    assert (second.tracks_pots, second.amount_pence) == (False, parse_money("1100"))
    assert (last.label, last.amount_pence, last.note) == ("Lloyds", None, "what is left")


def test_synthetic_renewals(synthetic):
    by_kind = {r.kind: r for r in synthetic.document.renewals}
    assert list(by_kind) == ["Car MOT", "Ollie SIM", "Boiler", "Gym"]
    mot = by_kind["Car MOT"]
    assert (mot.company, mot.expires_on, mot.comment) == ("", date(2026, 11, 6), "book early")
    sim = by_kind["Ollie SIM"]
    assert (sim.rolling, sim.expires_on) == (True, None)
    assert by_kind["Boiler"].expires_on == date(2025, 12, 9)
    assert (by_kind["Gym"].expires_on, by_kind["Gym"].rolling) == (None, False)


def test_synthetic_wealth(synthetic):
    doc = synthetic.document
    assert [a.company for a in doc.wealth_accounts] == ["Fidelity", "State"]
    fidelity, state = doc.wealth_accounts
    assert fidelity.notes == "workplace pension"
    snapshot = doc.latest_snapshot(fidelity.id)
    assert snapshot.as_of == date(2026, 4, 29)
    assert snapshot.current_pence == parse_money("20000")
    assert snapshot.yearly_projection_pence == parse_money("50000")
    assert snapshot.year_growth == pytest.approx(0.29)
    state_snapshot = doc.latest_snapshot(state.id)
    assert (state_snapshot.current_pence, state_snapshot.year_growth) == (None, None)
    assert calc.wealth_total(doc) == parse_money("20000")
    assert calc.projection_total(doc) == parse_money("59000")


def test_synthetic_drift_is_reported_and_spare_follows(synthetic):
    checks = {c.label: c for c in synthetic.checks}
    block = checks["Savings / spends"]
    assert (block.sheet, block.imported) == ("£1,572", "£1,600")
    assert "EXPECTED DIFFERENCE" in block.note
    assert "£28" in block.note
    assert calc.summary(synthetic.document).spare == parse_money("2150")
    assert "EXPECTED DIFFERENCE" in checks["Spare"].note


def test_synthetic_a_matching_block_carries_no_note(tmp_path):
    path = _build_workbook(tmp_path / "book.xlsx")
    book = open_for_edit(path)
    book["Outgoings"]["H13"] = 1600
    book.save(path)
    checks = {c.label: c for c in import_workbook(path, today=AS_OF).checks}
    assert checks["Savings / spends"].note == ""


def test_synthetic_a_mismatched_total_fails_its_check(tmp_path):
    """The guard is only worth having if it can fail."""
    path = _build_workbook(tmp_path / "book.xlsx")
    book = open_for_edit(path)
    book["Outgoings"]["B4"] = 5001
    book.save(path)
    checks = {c.label: c for c in import_workbook(path, today=AS_OF).checks}
    assert checks["Money in"].ok is False
    assert (checks["Money in"].sheet, checks["Money in"].imported) == ("£5,001", "£5,000")


def test_synthetic_what_was_skipped_is_named(synthetic):
    joined = " | ".join(synthetic.skipped)
    assert "'Volleyball' has no amount" in joined
    assert "'Ollie SIM' had a placeholder date of 2001-01-01" in joined
    assert "'Old ISA' has no 'as of' date" in joined
    assert "savings/spends block" in joined
    assert "I42" in joined


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (datetime(2026, 1, 2, 9, 30), date(2026, 1, 2)),
        (date(2026, 1, 2), date(2026, 1, 2)),
        (46000, date(2025, 12, 9)),
        (46000.75, date(2025, 12, 9)),  # a time of day is dropped
        ("next year", None),
    ],
)
def test_as_date_reads_every_shape_a_cell_comes_in(value, expected):
    assert _as_date(value) == expected
