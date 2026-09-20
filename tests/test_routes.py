"""The forms, end to end: submit, then read the document back.

Every assertion goes through the real store, so a handler that renders happily
but writes nothing fails here.
"""

from __future__ import annotations

from datetime import date

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


def test_payday_clears_the_dashboard_nag(client, stored):
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
    client.post("/monthly", data={f"transfer_{tracking.id}": "999"})

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


def test_add_a_remainder_transfer(client):
    client.post("/monthly/transfers/add", data={"label": "Spare", "amount": "", "note": "left"})
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
    before = len(stored.snapshots_for(pension.id))

    client.post(
        f"/wealth/{pension.id}/snapshot",
        data={"as_of": "2026-09-20", "current": "25000", "growth": "0.31"},
    )

    document = reload()
    assert len(document.snapshots_for(pension.id)) == before + 1
    latest = document.latest_snapshot(pension.id)
    assert latest.as_of == date(2026, 9, 20)
    assert latest.current_pence == 2_500_000


def test_a_fresh_valuation_clears_the_stale_warning(client):
    assert "out of date" in client.get("/").text
    for account in reload().wealth_accounts:
        client.post(
            f"/wealth/{account.id}/snapshot",
            data={"as_of": "2026-09-20", "current": "1000"},
        )
    assert "out of date" not in client.get("/").text


def test_add_and_delete_an_account_removes_its_snapshots(client):
    client.post("/wealth/accounts/add", data={"company": "A Fund", "planned": "1000"})
    account = next(a for a in reload().wealth_accounts if a.company == "A Fund")
    client.post(f"/wealth/{account.id}/snapshot", data={"as_of": "2026-09-01", "current": "500"})
    assert reload().snapshots_for(account.id)

    client.post(f"/wealth/accounts/{account.id}/delete")
    document = reload()
    assert account.id not in [a.id for a in document.wealth_accounts]
    # An orphaned snapshot would be invisible and counted by nothing.
    assert document.snapshots_for(account.id) == []


def test_bad_growth_value_does_not_500(client, stored):
    pension = stored.wealth_accounts[0]
    response = client.post(
        f"/wealth/{pension.id}/snapshot",
        data={"as_of": "2026-09-20", "current": "100", "growth": "not a number"},
    )
    assert response.status_code == 200
    assert reload().latest_snapshot(pension.id).year_growth is None


# --- general ------------------------------------------------------------------


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
