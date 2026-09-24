"""Shared fixtures.

Two things every test needs and neither of which is optional:

**The caches must be cleared.** `settings()`, `secret()`, `dotenv()` and
`credential()` are all cached for the life of the process — which is correct in
a container that starts, serves and exits, and wrong in a test session that
changes the environment between tests. Without the autouse fixture below, the
first test to read a setting fixes it for every test after it.

**Storage must point somewhere local.** With `STATE_CONTAINER_URL` empty the
store falls back to a file, so pointing `LOCAL_STATE_PATH` at a tmp path gives
each test its own document with no Azure and no mocking.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pytest

from finances import settings as settings_module
from finances.model import (
    Document,
    Line,
    Pot,
    PotEntry,
    Renewal,
    Transfer,
    WealthAccount,
    WealthSnapshot,
)

PASSCODE = "test-passcode"


@pytest.fixture
def passcode() -> str:
    """The passcode `_isolated` configures. A fixture rather than an import,
    because `tests/` is not a package and a relative import would not resolve."""
    return PASSCODE


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch) -> Iterator[None]:
    """Every test gets its own document, passcode and clean caches."""
    monkeypatch.setenv("APP_PASSCODE", PASSCODE)
    monkeypatch.setenv("LOCAL_STATE_PATH", str(tmp_path / "finances.json"))
    monkeypatch.delenv("STATE_CONTAINER_URL", raising=False)
    monkeypatch.delenv("KEY_VAULT_URI", raising=False)
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
    # `.env` belongs to whoever is running the command, and a developer's one
    # would otherwise leak into the test run.
    monkeypatch.setattr(settings_module, "dotenv", lambda: {})

    _clear_caches()
    yield
    _clear_caches()


def _clear_caches() -> None:
    settings_module.settings.cache_clear()
    settings_module.secret.cache_clear()
    settings_module.optional_secret.cache_clear()
    settings_module.credential.cache_clear()


@pytest.fixture
def today() -> date:
    """A fixed date, so "in 23 days" means the same thing on every run."""
    return date(2026, 9, 20)


@pytest.fixture
def doc(today: date) -> Document:
    """A small document exercising every collection.

    Deliberately not the sample from `seed.py`: this one is shaped for
    assertions — round numbers, one pot excluded from payday, one overdue
    renewal, one stale wealth figure.
    """
    document = Document()

    document.income = [
        Line(name="Salary", amount_pence=400_000),
        Line(name="Other", amount_pence=100_000),
        Line(name="Cancelled", amount_pence=50_000, active=False),
    ]
    document.outgoings = [
        Line(name="Mortgage", amount_pence=90_000),
        Line(name="Bills", amount_pence=10_000),
        Line(name="Old thing", amount_pence=5_000, active=False),
    ]

    holidays = Pot(name="Holidays", monthly_pence=45_000)
    car = Pot(name="Car", monthly_pence=5_000)
    general = Pot(name="Savings", monthly_pence=0, counts_toward_payday=False)
    document.pots = [holidays, car, general]

    document.pot_entries = [
        PotEntry(pot_id=holidays.id, on=date(2026, 8, 31), amount_pence=127_500, kind="opening"),
        PotEntry(pot_id=car.id, on=date(2026, 8, 31), amount_pence=20_000, kind="opening"),
        PotEntry(pot_id=car.id, on=date(2026, 9, 2), amount_pence=-30_000, kind="spend"),
        PotEntry(pot_id=general.id, on=date(2026, 8, 31), amount_pence=630_000, kind="opening"),
    ]

    document.transfers = [
        Transfer(label="Savings", amount_pence=None, tracks_pots=True),
        Transfer(label="Bills account", amount_pence=110_000),
        Transfer(label="Current", amount_pence=None, note="remainder"),
    ]

    document.renewals = [
        Renewal(kind="Car MOT", expires_on=date(2026, 11, 6)),
        Renewal(kind="Mortgage", company="A Bank", expires_on=date(2027, 5, 31)),
        Renewal(kind="SIM", company="A Telco", rolling=True),
    ]

    pension = WealthAccount(company="A Pension")
    isa = WealthAccount(company="An ISA")
    document.wealth_accounts = [pension, isa]
    document.wealth_snapshots = [
        WealthSnapshot(
            account_id=pension.id,
            as_of=date(2026, 4, 29),
            current_pence=2_367_900,
            yearly_projection_pence=25_500_000,
            year_growth=0.2903,
        ),
        WealthSnapshot(account_id=isa.id, as_of=date(2026, 7, 17), current_pence=5_255_600),
    ]

    return document


@pytest.fixture
def stored(doc: Document) -> Document:
    """`doc`, already written to the local file the app will read."""
    from finances import store

    store.save(doc)
    return doc


# **https, not http.** The session cookie is set `Secure`, and a cookie jar
# will not store one received over a plain-http origin — so a client on
# `http://testserver` logs in, is handed a cookie, silently drops it, and every
# subsequent request looks anonymous. That failure is invisible to any test that
# follows redirects and asserts only on the status code, because the redirect to
# /login renders a 200. It also matches production, where the platform
# terminates TLS and there is no plain-http route in.
BASE_URL = "https://testserver"


@pytest.fixture
def client(stored: Document):
    """A logged-in test client against a document that already has data."""
    from fastapi.testclient import TestClient

    from finances.api.main import create_app

    with TestClient(create_app(), base_url=BASE_URL) as test_client:
        response = test_client.post("/login", data={"passcode": PASSCODE})
        # Fail loudly here rather than leaving every test in the module to fail
        # on a redirect to /login.
        assert response.status_code == 200, "the fixture failed to log in"
        yield test_client


@pytest.fixture
def anonymous(stored: Document):
    """A client that has not presented the passcode."""
    from fastapi.testclient import TestClient

    from finances.api.main import create_app

    with TestClient(create_app(), base_url=BASE_URL, follow_redirects=False) as test_client:
        yield test_client
