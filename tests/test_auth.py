"""The passcode gate. It is the only thing between the internet and the money."""

from __future__ import annotations

import pytest

from finances.api import deps


def test_every_page_redirects_to_login_when_anonymous(anonymous):
    for path in ("/", "/payday", "/pots", "/monthly", "/renewals", "/wealth"):
        response = anonymous.get(path)
        # 303, not 401: a browser shown a 401 renders the error rather than the
        # login form.
        assert response.status_code == 303, path
        assert response.headers["location"] == "/login"


def test_probes_are_outside_the_gate(anonymous):
    """Container Apps' probes carry no cookie and must not be redirected."""
    assert anonymous.get("/healthz").status_code == 200
    assert anonymous.get("/readyz").status_code == 200


def test_the_icon_is_outside_the_gate(anonymous):
    """The login page links it, so a gated icon would never render."""
    for path in ("/icon.svg", "/favicon.ico", "/manifest.json"):
        assert anonymous.get(path).status_code == 200, path


def test_login_form_is_reachable(anonymous):
    assert anonymous.get("/login").status_code == 200


def test_wrong_passcode_is_refused(anonymous):
    response = anonymous.post("/login", data={"passcode": "nope"})
    assert response.status_code == 401
    assert deps.COOKIE_NAME not in response.cookies


def test_correct_passcode_sets_a_session(anonymous, passcode):
    response = anonymous.post("/login", data={"passcode": passcode})
    assert response.status_code == 303
    cookie = response.cookies.get(deps.COOKIE_NAME)
    assert cookie and deps.valid(cookie)


def test_logged_in_client_reaches_every_page(client):
    """Asserts on the body, not just the status.

    A redirect to /login renders a 200 when redirects are followed, so a status
    check alone passes whether or not the session actually works.
    """
    for path in ("/", "/payday", "/pots", "/monthly", "/renewals", "/wealth"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert 'name="passcode"' not in response.text, f"{path} bounced to the login form"


def test_logout_clears_the_session(client):
    assert client.get("/").status_code == 200
    client.post("/logout")
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303


@pytest.mark.parametrize("value", ["£50-a-month", "naïve", "plain-ascii"])
def test_non_ascii_passcodes_are_refused_not_crashed(monkeypatch, value):
    """`hmac.compare_digest` raises TypeError on non-ASCII `str`.

    gym-log turned a `£` in a passcode into a 500 that way. For an app about
    pounds sterling that is not a hypothetical, so both sides are encoded
    first.
    """
    from finances import settings as settings_module

    monkeypatch.setenv("APP_PASSCODE", value)
    settings_module.secret.cache_clear()

    assert deps.check_passcode(value) is True
    assert deps.check_passcode("something else") is False


def test_tampered_signature_is_rejected():
    token = deps.issue()
    issued, _, signature = token.partition(".")
    assert deps.valid(f"{issued}.{'0' * len(signature)}") is False


def test_expired_session_is_rejected(monkeypatch):
    """A zero lifetime expires a token the instant it is issued."""
    from finances import settings as settings_module

    token = deps.issue()
    assert deps.valid(token) is True

    monkeypatch.setenv("COOKIE_MAX_AGE_SECONDS", "0")
    settings_module.settings.cache_clear()
    assert deps.valid(token) is False


def test_malformed_tokens_are_rejected():
    for token in (None, "", "no-dot", "notanumber.abc", "."):
        assert deps.valid(token) is False


def test_rotating_the_passcode_invalidates_sessions(monkeypatch):
    """The signing key is derived from the passcode when COOKIE_SECRET is unset."""
    from finances import settings as settings_module

    token = deps.issue()
    assert deps.valid(token) is True

    monkeypatch.setenv("APP_PASSCODE", "a-different-passcode")
    settings_module.secret.cache_clear()
    assert deps.valid(token) is False
