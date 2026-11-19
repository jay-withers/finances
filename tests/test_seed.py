"""The sample document `finances seed` writes for local runs and browser tests."""

from __future__ import annotations

from datetime import date

import pytest

from finances import calc
from finances.seed import _months_back, sample_document


@pytest.mark.parametrize(
    ("today", "count", "expected"),
    [
        (date(2026, 11, 19), 0, date(2026, 11, 30)),
        (date(2026, 11, 19), 1, date(2026, 10, 31)),
        (date(2026, 1, 5), 1, date(2025, 12, 31)),
        (date(2026, 3, 31), 1, date(2026, 2, 28)),
        (date(2026, 11, 19), 12, date(2025, 11, 30)),
    ],
)
def test_months_back_is_the_last_day_of_that_month(today: date, count: int, expected: date):
    assert _months_back(today, count) == expected


def test_the_sample_has_a_year_of_history_and_leaves_this_months_payday():
    today = date(2026, 11, 19)
    doc = sample_document(today)

    # Eleven paydays, the latest last month, so the payday screen still has
    # its button this month.
    months = [run.month for run in doc.payday_runs]
    assert len(months) == 11 and months[-1] == "2026-10"
    assert doc.payday_run_for(calc.current_month(today)) is None
    claimed = {i for run in doc.payday_runs for i in run.entry_ids}
    assert claimed == {e.id for e in doc.pot_entries if e.kind == "payday"}

    # Every month of the insights window has something in it.
    flows = calc.monthly_flows(doc, today)
    assert all(f.paid_in or f.paid_out for f in flows)
    # And the verdicts the insights page can give are all on show.
    verdicts = {h.verdict for h in calc.pot_health(doc, today)}
    assert {"overdrawn", "running down", "growing"} <= verdicts


def test_this_months_spend_stays_in_this_month_early_in_the_month():
    doc = sample_document(date(2026, 11, 5))
    boiler = next(e for e in doc.pot_entries if e.note == "Boiler")
    assert boiler.on == date(2026, 11, 1)


def test_seeded_paydays_carry_the_note_a_real_one_does():
    doc = sample_document(date(2026, 11, 19))
    notes = {e.note for e in doc.pot_entries if e.kind == "payday"}
    assert "Payday 2026-10" in notes and "" not in notes
