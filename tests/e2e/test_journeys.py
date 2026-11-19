"""What a person does with finances, in a real browser."""

from __future__ import annotations

import uuid
from urllib.parse import urlparse

import pytest
from appkit import testing
from playwright.sync_api import Page, expect

from .conftest import PASSCODE

# Every screen in the nav, with the seeded document behind it. A page that
# renders but trips the CSP, or throws in its script, fails the autouse
# console check.
PAGES = ["/", "/today", "/payday", "/pots", "/monthly", "/renewals", "/wealth", "/insights"]


def test_a_page_without_a_session_goes_to_login(page: Page) -> None:
    page.goto("/pots")
    assert urlparse(page.url).path == "/login"


def test_the_wrong_passcode_is_refused_and_the_right_one_lets_you_in(
    page: Page, console_errors: list[str]
) -> None:
    page.goto("/login")
    page.fill('input[name="passcode"]', "not-it")
    # The refusal is a 401, which Chromium logs as a failed resource load.
    # Waited for, so it lands before the clear below rather than after it.
    with page.expect_console_message(lambda message: "401" in message.text):
        page.click('form[action="/login"] [type="submit"]')
    assert urlparse(page.url).path == "/login"
    assert all(
        error.startswith("Failed to load resource") and "401" in error for error in console_errors
    ), console_errors
    console_errors.clear()

    testing.log_in(page, PASSCODE)
    assert urlparse(page.url).path != "/login"


@pytest.mark.parametrize("path", PAGES)
def test_every_page_renders_cleanly(signed_in: Page, path: str) -> None:
    response = signed_in.goto(path)
    assert response is not None
    assert response.status == 200, path
    # goto follows redirects: a screen that bounced elsewhere would pass on
    # the page it landed on.
    assert urlparse(signed_in.url).path == path


def test_a_pot_opens_from_the_pots_list(signed_in: Page) -> None:
    page = signed_in
    page.goto("/pots")
    link = page.locator('a[href^="/pots/"]').first
    href = link.get_attribute("href")
    link.click()
    page.wait_for_load_state("load")
    assert urlparse(page.url).path == href


def test_a_recorded_payday_shows_as_already_run(signed_in: Page) -> None:
    page = signed_in
    page.goto("/payday")
    page.click('form[action="/payday/run"] [type="submit"]')
    page.wait_for_url("**/pots*")

    page.goto("/payday")
    expect(page.get_by_text("Payday already run for")).to_be_visible()


def test_a_new_pot_is_listed_after_a_reload(signed_in: Page) -> None:
    page = signed_in
    name = f"Holiday {uuid.uuid4().hex[:6]}"
    page.goto("/pots")
    page.get_by_text("Add a pot").click()  # the form sits in a closed <details>
    form = page.locator('form[action="/pots/add"]')
    form.locator('input[name="name"]').fill(name)
    form.locator('input[name="monthly"]').fill("25")
    form.locator('[type="submit"]').click()
    page.wait_for_load_state("load")

    page.goto("/pots")
    expect(page.get_by_text(name)).to_be_visible()
