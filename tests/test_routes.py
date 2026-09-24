"""The forms, end to end: submit, then read the document back.

Every assertion goes through the real store, so a handler that renders happily
but writes nothing fails here.
"""

from __future__ import annotations

from datetime import date

import pytest

from finances import calc, store
from finances.model import Document


def reload() -> Document:
    document, _etag = store.load()
    return document


def pot_named(document: Document, name: str):
    return next(p for p in document.pots if p.name == name)


# --- payday -------------------------------------------------------------------


def test_payday_run_tops_up_every_pot(client, stored):
    holidays = pot_named(stored, "Holidays")
    car = pot_named(stored, "Car")

    response = client.post(
        "/payday/run",
        data={
            "on": "2026-09-25",
            f"amount_{holidays.id}": "450",
            f"amount_{car.id}": "50",
        },
    )
    assert response.status_code == 200

    document = reload()
    balances = calc.pot_balances(document)
    assert balances[holidays.id] == 127_500 + 45_000
    assert balances[car.id] == -10_000 + 5_000

    run = document.payday_run_for("2026-09")
    assert run is not None
    assert run.run_on == date(2026, 9, 25)
    assert len(run.entry_ids) == 2


def test_payday_skips_a_pot_left_blank(client, stored):
    """An unusual month is recorded as what happened, not as the default."""
    holidays = pot_named(stored, "Holidays")
    car = pot_named(stored, "Car")

    client.post(
        "/payday/run",
        data={"on": "2026-09-25", f"amount_{holidays.id}": "450", f"amount_{car.id}": ""},
    )

    document = reload()
    assert calc.pot_balances(document)[car.id] == -10_000  # untouched
    assert len(document.payday_run_for("2026-09").entry_ids) == 1


def test_payday_accepts_an_unusual_amount(client, stored):
    holidays = pot_named(stored, "Holidays")
    client.post("/payday/run", data={"on": "2026-09-25", f"amount_{holidays.id}": "900"})
    assert calc.pot_balances(reload())[holidays.id] == 127_500 + 90_000


def test_rerunning_payday_replaces_rather_than_doubles(client, stored):
    """The correction path. Two runs for one month would double every balance."""
    holidays = pot_named(stored, "Holidays")
    form = {"on": "2026-09-25", f"amount_{holidays.id}": "450"}

    client.post("/payday/run", data=form)
    client.post("/payday/run", data=form)

    document = reload()
    assert calc.pot_balances(document)[holidays.id] == 127_500 + 45_000
    assert len(document.payday_runs) == 1
    assert len([e for e in document.pot_entries if e.kind == "payday"]) == 1


def test_rerunning_with_a_different_amount_corrects_it(client, stored):
    holidays = pot_named(stored, "Holidays")
    client.post("/payday/run", data={"on": "2026-09-25", f"amount_{holidays.id}": "450"})
    client.post("/payday/run", data={"on": "2026-09-25", f"amount_{holidays.id}": "500"})
    assert calc.pot_balances(reload())[holidays.id] == 127_500 + 50_000


def test_payday_zero_amount_is_treated_as_skipped(client, stored):
    """Typed as "0" rather than left blank, a pot still sits this month out."""
    holidays = pot_named(stored, "Holidays")
    client.post("/payday/run", data={"on": "2026-09-25", f"amount_{holidays.id}": "0"})

    document = reload()
    assert calc.pot_balances(document)[holidays.id] == 127_500  # untouched
    assert document.payday_run_for("2026-09").entry_ids == []


def test_payday_transfer_amount_is_saved(client, stored):
    fixed = next(t for t in stored.transfers if not t.tracks_pots and t.amount_pence is not None)
    client.post("/payday/transfers", data={f"transfer_{fixed.id}": "1250"})
    assert calc.transfer_amount(reload(), fixed.id) == 125_000


def test_payday_transfer_can_be_deleted(client, stored):
    fixed = next(t for t in stored.transfers if not t.tracks_pots and t.amount_pence is not None)
    client.post(f"/payday/transfers/{fixed.id}/delete")
    assert fixed.id not in [t.id for t in reload().transfers]


def test_payday_clears_the_dashboard_nag(client, stored, monkeypatch):
    """The nag only starts on the 28th, so the dashboard's clock is pinned here
    rather than relying on whatever day the suite happens to run on."""
    monkeypatch.setattr("finances.api.routes._today", lambda: date(2026, 9, 29))
    assert "Payday not yet run" in client.get("/").text
    holidays = pot_named(stored, "Holidays")
    client.post("/payday/run", data={"on": "2026-09-25", f"amount_{holidays.id}": "450"})
    assert "Payday not yet run" not in client.get("/").text


# --- pots ---------------------------------------------------------------------


def test_spend_is_stored_negative_whatever_sign_is_typed(client, stored):
    """ "100" and "-100" in the spend box must mean the same thing.

    Getting this wrong silently doubles a pot instead of halving it.
    """
    holidays = pot_named(stored, "Holidays")
    client.post(
        f"/pots/{holidays.id}/entry",
        data={"amount": "-100", "kind": "spend", "on": "2026-09-10", "note": "Deposit"},
    )
    assert calc.pot_balances(reload())[holidays.id] == 127_500 - 10_000


def test_deposit_is_stored_positive(client, stored):
    car = pot_named(stored, "Car")
    client.post(
        f"/pots/{car.id}/entry",
        data={"amount": "25", "kind": "deposit", "on": "2026-09-10", "note": "Refund"},
    )
    assert calc.pot_balances(reload())[car.id] == -10_000 + 2_500


def test_entry_can_be_deleted(client, stored):
    car = pot_named(stored, "Car")
    entry = reload().entries_for(car.id)[0]
    client.post(f"/pots/{car.id}/entry/{entry.id}/delete")
    assert entry.id not in [e.id for e in reload().pot_entries]


def test_deleting_a_payday_entry_detaches_it_from_the_run(client, stored):
    """Otherwise re-running the month would try to remove an entry already gone."""
    holidays = pot_named(stored, "Holidays")
    client.post("/payday/run", data={"on": "2026-09-25", f"amount_{holidays.id}": "450"})
    entry_id = reload().payday_run_for("2026-09").entry_ids[0]

    client.post(f"/pots/{holidays.id}/entry/{entry_id}/delete")
    assert reload().payday_run_for("2026-09").entry_ids == []

    # And re-running still works.
    client.post("/payday/run", data={"on": "2026-09-25", f"amount_{holidays.id}": "450"})
    assert calc.pot_balances(reload())[holidays.id] == 127_500 + 45_000


def test_add_and_edit_a_pot(client):
    client.post("/pots/add", data={"name": "New roof", "monthly": "75"})
    pot = pot_named(reload(), "New roof")
    assert pot.monthly_pence == 7_500
    assert pot.counts_toward_payday is True

    client.post(
        f"/pots/{pot.id}/edit",
        data={"name": "Roof", "monthly": "80", "counts": "1", "archived": "1"},
    )
    updated = next(p for p in reload().pots if p.id == pot.id)
    assert (updated.name, updated.monthly_pence, updated.archived) == ("Roof", 8_000, True)


def test_unchecking_counts_toward_payday_removes_it_from_the_run(client, stored):
    car = pot_named(stored, "Car")
    client.post(f"/pots/{car.id}/edit", data={"name": "Car", "monthly": "50"})
    assert "Car" not in [p.name for p in calc.payday_pots(reload())]


def test_unknown_pot_redirects_rather_than_500s(client):
    assert client.get("/pots/nope").status_code == 200


# --- the monthly picture ------------------------------------------------------


def test_bulk_save_updates_every_amount(client, stored):
    mortgage = next(line for line in stored.outgoings if line.name == "Mortgage")
    salary = next(line for line in stored.income if line.name == "Salary")

    client.post(
        "/monthly",
        data={
            f"amount_{mortgage.id}": "950",
            f"name_{mortgage.id}": "Mortgage",
            f"present_{mortgage.id}": "1",
            f"active_{mortgage.id}": "1",
            f"amount_{salary.id}": "4100",
            f"name_{salary.id}": "Salary",
            f"present_{salary.id}": "1",
            f"active_{salary.id}": "1",
        },
    )

    document = reload()
    assert (
        next(line for line in document.outgoings if line.id == mortgage.id).amount_pence == 95_000
    )
    assert next(line for line in document.income if line.id == salary.id).amount_pence == 410_000


def test_unticking_a_line_deactivates_it(client, stored):
    """An unchecked checkbox submits nothing, so `present_` is what marks the row."""
    bills = next(line for line in stored.outgoings if line.name == "Bills")
    client.post(
        "/monthly",
        data={
            f"amount_{bills.id}": "100",
            f"name_{bills.id}": "Bills",
            f"present_{bills.id}": "1",
            # no active_ key: unticked
        },
    )
    assert next(line for line in reload().outgoings if line.id == bills.id).active is False


def test_a_row_not_on_the_form_keeps_its_active_flag(client, stored):
    """Without `present_`, "unticked" and "not rendered" would look identical."""
    bills = next(line for line in stored.outgoings if line.name == "Bills")
    client.post("/monthly", data={f"amount_{bills.id}": "100"})
    assert next(line for line in reload().outgoings if line.id == bills.id).active is True


def test_the_pot_tracking_transfer_cannot_be_overwritten(client, stored):
    """The whole point: one source for the pot contributions, not two."""
    tracking = next(t for t in stored.transfers if t.tracks_pots)
    client.post("/payday/transfers", data={f"transfer_{tracking.id}": "999"})

    document = reload()
    updated = next(t for t in document.transfers if t.id == tracking.id)
    assert updated.amount_pence is None
    assert calc.transfer_amount(document, tracking.id) == 50_000


def test_add_and_delete_lines(client):
    client.post("/monthly/add", data={"kind": "outgoing", "name": "Water", "amount": "28.36"})
    line = next(line for line in reload().outgoings if line.name == "Water")
    assert line.amount_pence == 2_836

    client.post(f"/monthly/{line.id}/delete")
    assert "Water" not in [line.name for line in reload().outgoings]


def test_add_an_income_line(client):
    client.post("/monthly/add", data={"kind": "income", "name": "Bonus", "amount": "500"})
    line = next(line for line in reload().income if line.name == "Bonus")
    assert line.amount_pence == 50_000


def test_add_a_remainder_transfer(client):
    client.post("/payday/transfers/add", data={"label": "Spare", "amount": "", "note": "left"})
    transfer = next(t for t in reload().transfers if t.label == "Spare")
    assert transfer.amount_pence is None


# --- renewals -----------------------------------------------------------------


def test_add_edit_and_delete_a_renewal(client):
    client.post(
        "/renewals/add",
        data={"kind": "Boiler", "company": "A Gas Co", "expires_on": "2027-01-15"},
    )
    renewal = next(r for r in reload().renewals if r.kind == "Boiler")
    assert renewal.expires_on == date(2027, 1, 15)

    client.post(
        f"/renewals/{renewal.id}",
        data={
            "kind": "Boiler service",
            "company": "A Gas Co",
            "expires_on": "2027-02-01",
            "notice_days": "90",
        },
    )
    updated = next(r for r in reload().renewals if r.id == renewal.id)
    assert (updated.kind, updated.notice_days) == ("Boiler service", 90)

    client.post(f"/renewals/{renewal.id}/delete")
    assert renewal.id not in [r.id for r in reload().renewals]


def test_marking_a_renewal_rolling_clears_its_date(client, stored):
    """A rolling contract has no expiry, so it can never be due."""
    mot = next(r for r in stored.renewals if r.kind == "Car MOT")
    client.post(
        f"/renewals/{mot.id}",
        data={"kind": "Car MOT", "expires_on": "2026-11-06", "rolling": "1"},
    )
    updated = next(r for r in reload().renewals if r.id == mot.id)
    assert updated.rolling is True
    assert updated.expires_on is None


def test_a_due_renewal_shows_on_the_dashboard(client):
    assert "Car MOT" in client.get("/").text


# --- wealth -------------------------------------------------------------------


def test_recording_a_valuation_appends_rather_than_replaces(client, stored):
    """The trend is the point; the spreadsheet threw every previous reading away."""
    pension = stored.wealth_accounts[0]
    previous = stored.latest_snapshot(pension.id)
    before = len(stored.snapshots_for(pension.id))

    client.post(
        f"/wealth/{pension.id}/snapshot",
        data={"as_of": "2026-09-20", "current": "25000"},
    )

    document = reload()
    assert len(document.snapshots_for(pension.id)) == before + 1
    latest = document.latest_snapshot(pension.id)
    assert latest.as_of == date(2026, 9, 20)
    assert latest.current_pence == 2_500_000
    # Worked out from the previous snapshot rather than typed in.
    expected = calc.annualised_growth(previous, 2_500_000, date(2026, 9, 20))
    assert latest.year_growth == pytest.approx(expected)


def test_a_mistyped_valuation_can_be_deleted(client, stored):
    pension = stored.wealth_accounts[0]
    mistyped = stored.latest_snapshot(pension.id)

    client.post(f"/wealth/{pension.id}/snapshot/{mistyped.id}/delete")

    document = reload()
    assert mistyped.id not in [s.id for s in document.snapshots_for(pension.id)]


def test_deleting_a_valuation_under_the_wrong_account_is_a_noop(client, stored):
    """A stale form from a since-deleted account must not delete a reading
    that has since been reassigned to a different one."""
    pension, army = stored.wealth_accounts[0], stored.wealth_accounts[1]
    snapshot = stored.latest_snapshot(pension.id)

    client.post(f"/wealth/{army.id}/snapshot/{snapshot.id}/delete")

    document = reload()
    assert snapshot.id in [s.id for s in document.snapshots_for(pension.id)]


def test_a_fresh_valuation_clears_the_stale_warning(client):
    assert "out of date" in client.get("/").text
    for account in reload().wealth_accounts:
        client.post(
            f"/wealth/{account.id}/snapshot",
            data={"as_of": "2026-09-20", "current": "1000"},
        )
    assert "out of date" not in client.get("/").text


def test_a_drop_in_value_is_shown_in_the_bad_colour(client, stored):
    pension = stored.wealth_accounts[0]
    client.post(f"/wealth/{pension.id}/snapshot", data={"as_of": "2026-09-20", "current": "100"})
    text = client.get("/wealth").text
    assert "-£23,579" in text
    assert "var(--bad)" in text


def test_sparkline_handles_a_flat_valuation():
    """Two equal readings must not divide by a zero span."""
    from finances.api.routes import _sparkline
    from finances.model import WealthSnapshot

    flat = [
        WealthSnapshot(account_id="a", as_of=date(2026, 1, 1), current_pence=1_000),
        WealthSnapshot(account_id="a", as_of=date(2026, 4, 1), current_pence=1_000),
    ]
    points = _sparkline(flat)
    assert points is not None
    assert "inf" not in points.line and "nan" not in points.line
    assert "inf" not in points.area and "nan" not in points.area


def test_no_sparkline_with_only_one_valuation(client):
    """A single reading has no trend to draw."""
    assert "<svg" not in client.get("/wealth").text


def test_a_second_valuation_draws_a_sparkline(client, stored):
    pension = stored.wealth_accounts[0]
    client.post(f"/wealth/{pension.id}/snapshot", data={"as_of": "2026-09-20", "current": "25000"})
    text = client.get("/wealth").text
    assert "<polyline points=" in text
    assert "<polygon points=" in text
    # A rise from £23,679 to £25,000: shown as the change over the whole
    # tracked history, not just the latest step.
    assert "+£1,321" in text
    assert "since 29 Apr 26" in text


def test_add_and_delete_an_account_removes_its_snapshots(client):
    client.post("/wealth/accounts/add", data={"company": "A Fund"})
    account = next(a for a in reload().wealth_accounts if a.company == "A Fund")
    client.post(f"/wealth/{account.id}/snapshot", data={"as_of": "2026-09-01", "current": "500"})
    assert reload().snapshots_for(account.id)

    client.post(f"/wealth/accounts/{account.id}/delete")
    document = reload()
    assert account.id not in [a.id for a in document.wealth_accounts]
    # An orphaned snapshot would be invisible and counted by nothing.
    assert document.snapshots_for(account.id) == []


def test_editing_an_unknown_account_is_a_noop(client, stored):
    before = len(stored.wealth_accounts)
    response = client.post("/wealth/accounts/does-not-exist", data={"company": "Ghost"})
    assert response.status_code == 200
    assert len(reload().wealth_accounts) == before


def test_army_and_state_cannot_be_edited(client, stored):
    """There is exactly one of each; nothing about them is ever renamed."""
    army = next(a for a in stored.wealth_accounts if a.company == "Army")
    client.post(f"/wealth/accounts/{army.id}", data={"company": "Renamed"})
    assert reload().account(army.id).company == "Army"


def test_army_and_state_cannot_be_deleted(client, stored):
    army = next(a for a in stored.wealth_accounts if a.company == "Army")
    client.post(f"/wealth/accounts/{army.id}/delete")
    assert army.id in [a.id for a in reload().wealth_accounts]


def test_army_and_state_still_take_new_valuations(client, stored):
    """Editing the account is blocked; recording what it pays is not — that's
    how its yearly projection gets updated over time."""
    army = next(a for a in stored.wealth_accounts if a.company == "Army")
    client.post(f"/wealth/{army.id}/snapshot", data={"as_of": "2026-09-20", "projection": "9000"})
    latest = reload().latest_snapshot(army.id)
    assert latest.yearly_projection_pence == 900_000


def test_army_and_state_have_no_edit_account_section_on_the_page(client):
    text = client.get("/wealth").text
    army_start = text.index("<h2>Army")
    army_section = text[army_start : text.index("<h2>", army_start + 1)]
    assert "Edit account" not in army_section


def test_editing_an_account_updates_its_fields(client, stored):
    pension = stored.wealth_accounts[0]
    client.post(
        f"/wealth/accounts/{pension.id}",
        data={
            "company": "Fidelity Renamed",
            "notes": "moved provider",
            "still_contributing": "1",
            "target_retirement_year": "2051",
        },
    )
    updated = next(a for a in reload().wealth_accounts if a.id == pension.id)
    assert updated.company == "Fidelity Renamed"
    assert updated.notes == "moved provider"
    assert updated.still_contributing is True
    assert updated.target_retirement_year == 2051


def test_unticking_still_contributing_clears_it(client, stored):
    pension = stored.wealth_accounts[0]
    client.post(
        f"/wealth/accounts/{pension.id}",
        data={"company": pension.company, "still_contributing": "1"},
    )
    assert reload().account(pension.id).still_contributing is True

    # No `still_contributing` key at all: an unticked checkbox submits nothing.
    client.post(f"/wealth/accounts/{pension.id}", data={"company": pension.company})
    assert reload().account(pension.id).still_contributing is False


def test_still_contributing_accounts_are_listed_first(client, stored):
    """Newly marked as contributing, an account jumps ahead of the household's
    fixed, never-contributing ones."""
    dormant = next(a for a in stored.wealth_accounts if a.company == "Army")
    client.post(
        "/wealth/accounts/add", data={"company": "A New Pension", "still_contributing": "1"}
    )
    text = client.get("/wealth").text
    assert text.index("A New Pension") < text.index(dormant.company)


def test_a_target_retirement_year_estimates_a_value_once_growth_is_known(client):
    """The stand-in for a figure a defined-contribution pension never states outright."""
    client.post("/wealth/accounts/add", data={"company": "A Fund"})
    account = next(a for a in reload().wealth_accounts if a.company == "A Fund")
    client.post(
        f"/wealth/accounts/{account.id}",
        data={"company": account.company, "target_retirement_year": "2050"},
    )

    client.post(f"/wealth/{account.id}/snapshot", data={"as_of": "2026-01-01", "current": "1000"})
    # A first valuation has no previous figure to grow from, so no rate to compound.
    assert "Projected at retirement" not in client.get("/wealth").text

    client.post(f"/wealth/{account.id}/snapshot", data={"as_of": "2026-09-20", "current": "1100"})
    assert "Projected at retirement" in client.get("/wealth").text


def test_the_wealth_page_headline_never_counts_an_estimated_pot_value(client, stored):
    """A lump-sum pot estimate and an annual income figure are different
    kinds of quantity: the headline "Yearly projection" total must stay the
    known, defined-benefit-style figures, even once an account gets an
    estimate of its own."""
    headline = '<div class="label">Yearly projection</div><div class="value">£20,000</div>'
    assert headline in client.get("/wealth").text

    client.post("/wealth/accounts/add", data={"company": "A Fund"})
    account = next(a for a in reload().wealth_accounts if a.company == "A Fund")
    client.post(
        f"/wealth/accounts/{account.id}",
        data={"company": account.company, "target_retirement_year": "2060"},
    )
    client.post(f"/wealth/{account.id}/snapshot", data={"as_of": "2026-01-01", "current": "1000"})
    client.post(f"/wealth/{account.id}/snapshot", data={"as_of": "2026-09-20", "current": "1100"})

    text = client.get("/wealth").text
    assert "Projected at retirement" in text  # the account's own row shows it...
    assert headline in text  # ...but the headline total is unmoved


def test_a_first_valuation_has_no_previous_figure_to_grow_from(client):
    client.post("/wealth/accounts/add", data={"company": "A Fund"})
    account = next(a for a in reload().wealth_accounts if a.company == "A Fund")

    response = client.post(
        f"/wealth/{account.id}/snapshot", data={"as_of": "2026-09-20", "current": "100"}
    )
    assert response.status_code == 200
    assert reload().latest_snapshot(account.id).year_growth is None


def test_snapshot_for_an_unknown_account_is_a_noop(client, stored):
    before = len(stored.wealth_snapshots)
    response = client.post(
        "/wealth/does-not-exist/snapshot", data={"as_of": "2026-09-20", "current": "100"}
    )
    assert response.status_code == 200
    assert len(reload().wealth_snapshots) == before


# --- general ------------------------------------------------------------------


def test_a_conflicting_write_is_reported_as_409(client, stored, monkeypatch):
    """The one retry in `store.update` already failed twice; the caller must know."""
    from finances import store

    def always_conflicts(_change):
        raise store.ConflictError("the document changed while this change was being written")

    monkeypatch.setattr(store, "update", always_conflicts)
    response = client.post("/monthly/add", data={"kind": "outgoing", "name": "Water", "amount": ""})
    assert response.status_code == 409
    assert "changed" in response.json()["detail"]


def test_a_blank_date_falls_back_to_today_rather_than_500ing(client, stored):
    """`<input type="date">` submits an empty string when left blank."""
    car = pot_named(stored, "Car")
    response = client.post(
        f"/pots/{car.id}/entry", data={"amount": "10", "kind": "deposit", "on": ""}
    )
    assert response.status_code == 200
    assert reload().entries_for(car.id)[0].amount_pence == 1_000


def test_every_write_redirects(client, stored):
    """A reload after a write must not offer to submit it again."""
    car = pot_named(stored, "Car")
    response = client.post(
        f"/pots/{car.id}/entry",
        data={"amount": "1", "kind": "deposit"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/pots/{car.id}"


# --- icon ---------------------------------------------------------------------


def test_favicon_is_served_rather_than_404ing(client):
    """The one request a browser makes that nothing in the HTML asks for."""
    response = client.get("/favicon.ico")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert response.text.startswith("<svg")


def test_icon_and_favicon_are_the_same_bytes(client):
    assert client.get("/icon.svg").content == client.get("/favicon.ico").content


def test_manifest_names_an_icon(client):
    """An installable manifest with no icon is one Chrome declines to install."""
    icons = client.get("/manifest.json").json()["icons"]
    assert [i["src"] for i in icons] == ["/icon.svg"]
    assert icons[0]["sizes"] == "any"
