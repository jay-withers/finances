"""The forms, end to end: submit, then read the document back.

Every assertion goes through the real store, so a handler that renders happily
but writes nothing fails here.
"""

from __future__ import annotations

from datetime import date

import pytest

from finances import calc, store
from finances.model import Document, PaydayRun, WealthAccount, WealthSnapshot


# Lookups that return None for "not there". In a test that expects it there,
# fail on that line rather than with an AttributeError on the next.
def _run(document: Document, month: str) -> PaydayRun:
    run = document.payday_run_for(month)
    assert run is not None, f"no payday run for {month}"
    return run


def _account(document: Document, account_id: str) -> WealthAccount:
    account = document.account(account_id)
    assert account is not None, f"no account {account_id}"
    return account


def _latest(document: Document, account_id: str) -> WealthSnapshot:
    snapshot = document.latest_snapshot(account_id)
    assert snapshot is not None, f"no snapshot for {account_id}"
    return snapshot


def reload() -> Document:
    document, _etag = store.load()
    return document


def pot_named(document: Document, name: str):
    return next(p for p in document.pots if p.name == name)


# --- home ---------------------------------------------------------------------


def _home(client, monkeypatch, today: date) -> str:
    monkeypatch.setattr("finances.api.routes._today", lambda: today)
    return client.get("/").text


def _tile(html: str, title: str) -> str:
    """The HTML of one tile, from its label to the next tile."""
    start = html.index(f'<div class="label">{title}</div>')
    opening = html.rindex("<a ", 0, start)
    return html[opening : html.index("</a>", start)]


def test_home_is_a_tile_per_area_and_nothing_else(client, monkeypatch):
    html = _home(client, monkeypatch, date(2026, 9, 20))
    for title, href in [
        ("Today", "/today"),
        ("Payday", "/payday"),
        ("Savings", "/pots"),
        ("In / Out", "/monthly"),
        ("Renewals", "/renewals"),
        ("Pensions", "/wealth"),
        ("Insights", "/insights"),
    ]:
        assert f'href="{href}"' in _tile(html, title), title
    # The attention list and the week's movements live on Today now.
    assert "Needs attention" not in html and "Last 7 days" not in html
    assert "/logout" in html


def test_the_today_tile_counts_what_needs_attention(client, monkeypatch):
    # 29 Sep: payday a day late, the MOT due, pensions stale, Car overdrawn.
    tile = _tile(_home(client, monkeypatch, date(2026, 9, 29)), "Today")
    assert "4 things to look at" in tile
    assert "Payday not yet run for September 2026, and 3 more" in tile  # the first first
    assert "urgent" in tile


def test_the_today_tile_is_all_clear_with_nothing_to_do(client, monkeypatch):
    store.save(Document())
    tile = _tile(_home(client, monkeypatch, date(2026, 9, 29)), "Today")
    assert "All clear" in tile and "0 movements in the last 7 days" in tile
    assert "urgent" not in tile and "warn" not in tile
    html = _home(client, monkeypatch, date(2026, 9, 29))
    assert "none coming up" in _tile(html, "Renewals")
    assert "No pots to top up" in _tile(html, "Payday")
    assert "None yet" in _tile(html, "Pensions")


def test_the_payday_tile_follows_the_month(client, stored, monkeypatch):
    before = _tile(_home(client, monkeypatch, date(2026, 9, 20)), "Payday")
    assert "Due 28 Sep" in before and "£500 to top up" in before
    assert "warn" not in before

    on_the_day = _tile(_home(client, monkeypatch, date(2026, 9, 28)), "Payday")
    assert "Not yet run" in on_the_day and "warn" in on_the_day

    late = _tile(_home(client, monkeypatch, date(2026, 9, 29)), "Payday")
    assert "urgent" in late

    holidays = pot_named(stored, "Holidays")
    client.post("/payday/run", data={"on": "2026-09-29", f"amount_{holidays.id}": "450"})
    done = _tile(_home(client, monkeypatch, date(2026, 9, 30)), "Payday")
    assert "Done for September" in done and "run 29 Sep" in done


def test_an_overdrawn_pot_shows_its_balance_in_pounds_not_a_float(client, monkeypatch):
    monkeypatch.setattr("finances.api.routes._today", lambda: date(2026, 9, 20))
    assert "balance -£100" in client.get("/today").text


def test_the_savings_tile_flags_an_overdrawn_pot(client, monkeypatch):
    tile = _tile(_home(client, monkeypatch, date(2026, 9, 20)), "Savings")
    assert "£7,475" in tile  # every live pot
    assert "1 pot overdrawn" in tile and "urgent" in tile


def test_the_money_tile_shows_spare_or_the_shortfall(client, monkeypatch):
    tile = _tile(_home(client, monkeypatch, date(2026, 9, 20)), "In / Out")
    assert "£2,400" in tile and "spare a month" in tile
    client.post("/monthly/add", data={"kind": "outgoing", "name": "Yacht", "amount": "9000"})
    tile = _tile(_home(client, monkeypatch, date(2026, 9, 20)), "In / Out")
    assert "a month more out than in" in tile and "urgent" in tile


def test_the_renewals_tile_names_the_next_one(client, monkeypatch):
    soon = _tile(_home(client, monkeypatch, date(2026, 10, 7)), "Renewals")
    assert "Car MOT" in soon and "in 30 days" in soon and "warn" in soon
    # Red from two weeks out, matching the Today tile for the same renewal.
    close = _tile(_home(client, monkeypatch, date(2026, 10, 23)), "Renewals")
    assert "in 14 days" in close and "urgent" in close
    # Once expired the MOT stays named, since it is what needs dealing with.
    expired = _tile(_home(client, monkeypatch, date(2026, 11, 8)), "Renewals")
    assert "Car MOT" in expired and "expired 2 days ago" in expired and "urgent" in expired

    mot = next(r for r in reload().renewals if r.kind == "Car MOT")
    client.post(f"/renewals/{mot.id}", data={"kind": "Car MOT", "expires_on": "2027-11-06"})
    later = _tile(_home(client, monkeypatch, date(2026, 11, 8)), "Renewals")
    assert "Mortgage" in later and "in 204 days" in later and "warn" not in later
    all_expired = _tile(_home(client, monkeypatch, date(2028, 1, 1)), "Renewals")
    assert "expired" in all_expired and "urgent" in all_expired


def test_the_pensions_tile_says_when_figures_are_stale(client, monkeypatch):
    tile = _tile(_home(client, monkeypatch, date(2026, 9, 20)), "Pensions")
    assert "figures out of date" in tile and "warn" in tile
    fresh = _tile(_home(client, monkeypatch, date(2026, 5, 1)), "Pensions")
    assert "3 accounts" in fresh and "warn" not in fresh


def test_the_today_page_has_the_attention_list_and_the_week(client, monkeypatch):
    monkeypatch.setattr("finances.api.routes._today", lambda: date(2026, 9, 4))
    html = client.get("/today").text
    assert "Needs attention" in html and "Last 7 days" in html
    assert "Car" in html  # the 2 Sep spend, inside the week


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

    run = _run(document, "2026-09")
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
    assert len(_run(document, "2026-09").entry_ids) == 1


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
    assert _run(document, "2026-09").entry_ids == []


def test_payday_transfer_amount_is_saved(client, stored):
    fixed = next(t for t in stored.transfers if not t.tracks_pots and t.amount_pence is not None)
    client.post("/payday/transfers", data={f"transfer_{fixed.id}": "1250"})
    assert calc.transfer_amount(reload(), fixed.id) == 125_000


def test_payday_transfer_can_be_deleted(client, stored):
    fixed = next(t for t in stored.transfers if not t.tracks_pots and t.amount_pence is not None)
    client.post(f"/payday/transfers/{fixed.id}/delete")
    assert fixed.id not in [t.id for t in reload().transfers]


def test_payday_clears_the_today_nag(client, stored, monkeypatch):
    """The nag only starts on the 28th, so the Today page's clock is pinned here
    rather than relying on whatever day the suite happens to run on."""
    monkeypatch.setattr("finances.api.routes._today", lambda: date(2026, 9, 29))
    assert "Payday not yet run" in client.get("/today").text
    holidays = pot_named(stored, "Holidays")
    client.post("/payday/run", data={"on": "2026-09-25", f"amount_{holidays.id}": "450"})
    assert "Payday not yet run" not in client.get("/today").text


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
    entry_id = _run(reload(), "2026-09").entry_ids[0]

    client.post(f"/pots/{holidays.id}/entry/{entry_id}/delete")
    assert _run(reload(), "2026-09").entry_ids == []

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


def test_pots_page_lists_largest_balance_first(client, stored):
    balances = calc.pot_balances(stored)
    expected = [p.name for p in sorted(stored.pots, key=lambda p: balances[p.id], reverse=True)]

    html = client.get("/pots").text

    positions = [html.index(f">{name}</a>") for name in expected]
    assert positions == sorted(positions)
    assert "/move" not in html


def test_pot_page_groups_its_ledger_by_month(client, stored):
    car = pot_named(stored, "Car")
    client.post(
        f"/pots/{car.id}/entry",
        data={"amount": "12.34", "kind": "deposit", "on": "2026-10-05", "note": "refund"},
    )

    html = client.get(f"/pots/{car.id}").text

    # Newest month first, and only the newest open.
    assert html.index("October 2026") < html.index("September 2026") < html.index("August 2026")
    assert html.count('" open>') == 1
    assert "in £12.34" in html
    assert "imported £200" in html
    assert "refund" in html
    assert f"/pots/{car.id}/entry/" in html  # the delete buttons survive


def test_deleting_an_entry_comes_back_to_its_month(client, stored):
    car = pot_named(stored, "Car")
    for on in ("2026-09-10", "2026-09-11", "2026-10-05"):
        client.post(f"/pots/{car.id}/entry", data={"amount": "1", "on": on})
    doomed = next(e for e in reload().pot_entries if e.on == date(2026, 9, 10))

    response = client.post(
        f"/pots/{car.id}/entry/{doomed.id}/delete",
        data={"month": "2026-09"},
        follow_redirects=False,
    )
    assert response.headers["location"] == f"/pots/{car.id}?month=2026-09"

    html = client.get(response.headers["location"]).text
    assert html.count('<details class="month"') == 3
    assert '<details class="month" id="month-2026-09" open' in html
    assert '<details class="month" id="month-2026-10" open' not in html


def test_a_month_not_in_the_ledger_opens_the_newest(client, stored):
    car = pot_named(stored, "Car")
    for month in ("nonsense", "2020-01"):
        html = client.get(f"/pots/{car.id}?month={month}").text
        assert '<details class="month" id="month-2026-09" open' in html


def test_a_month_that_is_not_a_month_is_not_put_in_the_redirect(client, stored):
    car = pot_named(stored, "Car")
    entry = next(e for e in stored.pot_entries if e.pot_id == car.id)
    response = client.post(
        f"/pots/{car.id}/entry/{entry.id}/delete",
        data={"month": "x\r\nSet-Cookie: a=b"},
        follow_redirects=False,
    )
    assert response.headers["location"] == f"/pots/{car.id}"


def test_pot_page_with_no_entries_says_so(client):
    client.post("/pots/add", data={"name": "Empty", "monthly": ""})
    pot = pot_named(reload(), "Empty")
    assert "Nothing recorded yet." in client.get(f"/pots/{pot.id}").text


def test_pots_page_has_every_pot_by_month(client, stored):
    html = client.get("/pots").text
    by_month = html[html.index("By month") :]
    assert "September 2026" in by_month and "August 2026" in by_month
    # Labelled with the pot it belongs to, and no delete from here.
    assert ">Car</a>" in by_month
    assert "/delete" not in by_month
    assert "imported" in by_month
    # "out" says the direction; a minus as well would read as money coming back.
    assert "out £300" in by_month and "out -£300" not in by_month


def test_unknown_pot_redirects_rather_than_500s(client):
    assert client.get("/pots/nope").status_code == 200


# --- insights -----------------------------------------------------------------


@pytest.fixture
def november(monkeypatch):
    monkeypatch.setattr("finances.api.routes._today", lambda: date(2026, 11, 5))


def test_insights_shows_the_year_the_pots_and_the_split(client, stored, november):
    html = client.get("/insights").text

    assert "Saved, 12 months" in html
    assert "-£300" in html  # the one spend; the imports aren't saving
    assert "September 2026: £7,475" in html  # the balance chart's tip for that month
    assert "September 2026: in £0, out £300, net -£300" in html
    # Every pot, linked, with a verdict; Car is overdrawn by its spend.
    for pot in stored.pots:
        assert f'href="/pots/{pot.id}"' in html
    assert "overdrawn" in html
    # Income 5000: outgoings 1000, savings and spends 1600, spare 2400.
    assert "Outgoings £1,000" in html and "20%" in html
    assert "Spare £2,400" in html and "48%" in html


def test_the_average_month_leaves_out_the_month_in_progress(client, stored, november):
    """Before payday the current month nets about nothing, and counting it
    made the average read about a twelfth low."""
    car = pot_named(stored, "Car")
    client.post(f"/pots/{car.id}/entry", data={"amount": "1100", "on": "2026-11-02"})
    html = client.get("/insights").text
    figure = html[html.index("Average full month") :]
    assert "-£27.27" in figure[:200]  # the September spend over 11 full months


def test_insights_says_when_the_plan_spends_more_than_comes_in(client, stored, november):
    client.post("/monthly/add", data={"kind": "outgoing", "name": "Yacht", "amount": "9000"})
    html = client.get("/insights").text
    assert "more than comes in" in html
    assert 'class="split"' not in html


def test_insights_with_nothing_in_the_document(client, november):
    store.save(Document())
    html = client.get("/insights").text
    assert "No pots yet." in html
    assert "No income recorded." in html


def test_the_nav_links_to_insights(client):
    assert 'href="/insights"' in client.get("/pots").text


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


def test_a_due_renewal_shows_on_the_today_page(client):
    assert "Car MOT" in client.get("/today").text


# --- wealth -------------------------------------------------------------------


def test_recording_a_valuation_appends_rather_than_replaces(client, stored):
    """The trend is the point; the spreadsheet threw every previous reading away."""
    pension = stored.wealth_accounts[0]
    history = stored.snapshots_for(pension.id)
    before = len(history)

    client.post(
        f"/wealth/{pension.id}/snapshot",
        data={"as_of": "2026-09-20", "current": "25000"},
    )

    document = reload()
    assert len(document.snapshots_for(pension.id)) == before + 1
    latest = _latest(document, pension.id)
    assert latest.as_of == date(2026, 9, 20)
    assert latest.current_pence == 2_500_000
    # Worked out from the account's history rather than typed in.
    expected = calc.annualised_growth(history, 2_500_000, date(2026, 9, 20))
    assert latest.year_growth == pytest.approx(expected)


@pytest.mark.parametrize(
    ("current", "pence"),
    [
        # A digit too many, the day after the last reading: annualising a 10x
        # day overflowed a float.
        ("10000", 1_000_000),
        # An account that's gone negative: the growth came out complex.
        ("-50", -5_000),
    ],
)
def test_a_valuation_whose_growth_means_nothing_still_saves(client, current, pence):
    """Both used to be a 500, so the reading couldn't be recorded at all."""

    def one_reading(document: Document) -> Document:
        account = WealthAccount(company="Fresh")
        document.wealth_accounts.append(account)
        document.wealth_snapshots.append(
            WealthSnapshot(account_id=account.id, as_of=date(2026, 9, 19), current_pence=100_000)
        )
        return document

    account = store.update(one_reading).wealth_accounts[-1]

    response = client.post(
        f"/wealth/{account.id}/snapshot", data={"as_of": "2026-09-20", "current": current}
    )

    assert response.status_code == 200
    latest = _latest(reload(), account.id)
    assert latest.current_pence == pence
    assert latest.year_growth is None


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
    assert "out of date" in client.get("/today").text
    for account in reload().wealth_accounts:
        client.post(
            f"/wealth/{account.id}/snapshot",
            data={"as_of": "2026-09-20", "current": "1000"},
        )
    assert "out of date" not in client.get("/today").text


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
    assert _account(reload(), army.id).company == "Army"


def test_army_and_state_cannot_be_deleted(client, stored):
    army = next(a for a in stored.wealth_accounts if a.company == "Army")
    client.post(f"/wealth/accounts/{army.id}/delete")
    assert army.id in [a.id for a in reload().wealth_accounts]


def test_army_and_state_still_take_new_valuations(client, stored):
    """Editing the account is blocked; recording what it pays is not — that's
    how its yearly projection gets updated over time."""
    army = next(a for a in stored.wealth_accounts if a.company == "Army")
    client.post(f"/wealth/{army.id}/snapshot", data={"as_of": "2026-09-20", "projection": "9000"})
    latest = _latest(reload(), army.id)
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


def test_editing_an_account_sets_an_assumed_growth_rate(client, stored):
    pension = stored.wealth_accounts[0]
    client.post(
        f"/wealth/accounts/{pension.id}",
        data={"company": pension.company, "assumed_growth_rate": "4.5"},
    )
    assert _account(reload(), pension.id).assumed_growth_rate == pytest.approx(0.045)

    # Blank clears it back to the observed rate.
    client.post(f"/wealth/accounts/{pension.id}", data={"company": pension.company})
    assert _account(reload(), pension.id).assumed_growth_rate is None


def test_unticking_still_contributing_clears_it(client, stored):
    pension = stored.wealth_accounts[0]
    client.post(
        f"/wealth/accounts/{pension.id}",
        data={"company": pension.company, "still_contributing": "1"},
    )
    assert _account(reload(), pension.id).still_contributing is True

    # No `still_contributing` key at all: an unticked checkbox submits nothing.
    client.post(f"/wealth/accounts/{pension.id}", data={"company": pension.company})
    assert _account(reload(), pension.id).still_contributing is False


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

    row = "<td>Projected pot</td>"
    client.post(f"/wealth/{account.id}/snapshot", data={"as_of": "2026-01-01", "current": "1000"})
    # A first valuation has no previous figure to grow from, so no rate to compound.
    assert row not in client.get("/wealth").text

    client.post(f"/wealth/{account.id}/snapshot", data={"as_of": "2026-09-20", "current": "1100"})
    assert row in client.get("/wealth").text


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
    assert "<td>Projected pot</td>" in text  # the account's own row shows it...
    assert headline in text  # ...but the "Yearly projection" headline is unmoved


def test_the_retirement_estimate_headline_counts_estimated_accounts(client, stored):
    """The lump-sum counterpart to the "Yearly projection" headline, kept
    as its own separate total for the same reason the two rows are never
    merged."""
    zero = '<div class="label">Projected pot</div>\n    <div class="value">£0</div>'
    assert zero in client.get("/wealth").text

    client.post("/wealth/accounts/add", data={"company": "A Fund"})
    account = next(a for a in reload().wealth_accounts if a.company == "A Fund")
    client.post(
        f"/wealth/accounts/{account.id}",
        data={"company": account.company, "target_retirement_year": "2060"},
    )
    client.post(f"/wealth/{account.id}/snapshot", data={"as_of": "2026-01-01", "current": "1000"})
    client.post(f"/wealth/{account.id}/snapshot", data={"as_of": "2026-09-20", "current": "1100"})

    assert zero not in client.get("/wealth").text


def test_a_first_valuation_has_no_previous_figure_to_grow_from(client):
    client.post("/wealth/accounts/add", data={"company": "A Fund"})
    account = next(a for a in reload().wealth_accounts if a.company == "A Fund")

    response = client.post(
        f"/wealth/{account.id}/snapshot", data={"as_of": "2026-09-20", "current": "100"}
    )
    assert response.status_code == 200
    assert _latest(reload(), account.id).year_growth is None


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


def test_manifest_names_android_icons(client):
    """Android Chrome installs from a raster icon of at least 192px, and wants a
    separate maskable one for the launcher's own shape."""
    icons = client.get("/manifest.json").json()["icons"]
    assert {(i["sizes"], i["purpose"]) for i in icons} == {
        ("192x192", "any"),
        ("512x512", "any"),
        ("512x512", "maskable"),
    }
    assert all(i["type"] == "image/png" for i in icons)


def test_manifest_icons_are_served_outside_the_gate(anonymous):
    """The login page is where a fresh install starts, before any cookie."""
    for icon in anonymous.get("/manifest.json").json()["icons"]:
        response = anonymous.get(icon["src"])
        assert response.status_code == 200, icon["src"]
        assert response.headers["content-type"] == "image/png"
        assert response.content.startswith(b"\x89PNG")


def test_unknown_icon_is_a_404(anonymous):
    assert anonymous.get("/icons/nope.png").status_code == 404
