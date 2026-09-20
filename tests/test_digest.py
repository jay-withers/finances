"""The scheduled reminder digest."""

from __future__ import annotations

from datetime import date

import pytest

from finances import digest, mailer
from finances.model import Document, PaydayRun, Pot, Renewal


class FakeResponse:
    def __init__(self, payload=None, error=None):
        self._payload = payload or {"id": "abc123"}
        self._error = error

    def raise_for_status(self):
        if self._error:
            raise self._error

    def json(self):
        return self._payload


class FakeClient:
    """Records the one request the mailer makes."""

    def __init__(self, response=None):
        self.response = response or FakeResponse()
        self.calls = []

    def post(self, url, json=None, headers=None):
        self.calls.append((url, json, headers))
        return self.response


def test_digest_names_everything_that_needs_attention(doc: Document):
    # On the 28th: on or after the household's payday, so the nag is live.
    composed = digest.compose(doc, date(2026, 9, 28))
    assert "Car MOT" in composed.text
    assert "in 39 days" in composed.text
    assert "Payday not yet run" in composed.text
    assert "out of date" in composed.text
    assert composed.item_count >= 3
    assert "need attention" in composed.subject


def test_a_quiet_digest_still_says_what_it_checked(today: date):
    """The platform cannot see a schedule that never fires.

    `azure-container-apps` names that as its known gap, so a run with nothing to
    report has to say so rather than produce an empty message.
    """
    document = Document(
        pots=[Pot(name="Holidays", monthly_pence=1000)],
        renewals=[Renewal(kind="Far off", expires_on=date(2029, 1, 1))],
        payday_runs=[PaydayRun(month="2026-09", run_on=today)],
    )
    composed = digest.compose(document, today)
    assert composed.subject == "Finances — nothing due"
    assert "Nothing needs attention" in composed.text
    assert "Checked 1 renewal(s)" in composed.text
    assert composed.item_count == 0


def test_digest_carries_the_monthly_figures(doc: Document, today: date):
    composed = digest.compose(doc, today)
    assert "£5,000" in composed.text  # income
    assert "Spare" in composed.text


def test_html_and_text_carry_the_same_items(doc: Document, today: date):
    """Neither half of a multipart email may say less than the other."""
    from finances import calc

    for item in calc.attention(doc, today).items:
        assert item.title in composed_html(doc, today), item.title
        assert item.title in digest.compose(doc, today).text, item.title


def composed_html(doc: Document, today: date) -> str:
    return digest.compose(doc, today).html


def test_html_escapes_user_supplied_text(today: date):
    """Pot and renewal names are typed by a person and land in the email."""
    document = Document(renewals=[Renewal(kind="<script>alert(1)</script>", expires_on=today)])
    composed = digest.compose(document, today)
    assert "<script>" not in composed.html
    assert "&lt;script&gt;" in composed.html


def test_app_url_is_included_when_given(doc: Document, today: date):
    composed = digest.compose(doc, today, app_url="https://finances.example.uk")
    assert "https://finances.example.uk" in composed.text
    assert 'href="https://finances.example.uk"' in composed.html


def test_send_skips_with_no_recipient(monkeypatch):
    """The default. A job that emails on every dev run gets muted by its reader."""
    monkeypatch.delenv("DIGEST_EMAIL_TO", raising=False)
    client = FakeClient()
    result = mailer.send("subject", "<p>html</p>", "text", client=client)
    assert result.status == "skipped"
    assert client.calls == []


def test_send_posts_to_resend(monkeypatch):
    monkeypatch.setenv("DIGEST_EMAIL_TO", "a@example.com, b@example.com")
    monkeypatch.setenv("RESEND_API_KEY", "key-123")
    client = FakeClient()

    result = mailer.send("Finances — 2 things", "<p>html</p>", "text", client=client)

    assert result.status == "sent"
    assert result.provider_id == "abc123"
    url, payload, headers = client.calls[0]
    assert url == mailer.RESEND_ENDPOINT
    # Resend takes a list even for one recipient, and the addresses are trimmed.
    assert payload["to"] == ["a@example.com", "b@example.com"]
    assert payload["subject"] == "Finances — 2 things"
    assert headers["Authorization"] == "Bearer key-123"


def test_a_failed_send_is_reported_not_raised(monkeypatch):
    """A mail provider's bad minute must not become a job-failure alert."""
    monkeypatch.setenv("DIGEST_EMAIL_TO", "a@example.com")
    monkeypatch.setenv("RESEND_API_KEY", "key-123")
    client = FakeClient(FakeResponse(error=RuntimeError("502 Bad Gateway")))

    result = mailer.send("subject", "<p>html</p>", "text", client=client)

    assert result.status == "failed"
    assert "502" in result.error


def test_run_dry_run_prints_and_sends_nothing(stored, monkeypatch, capsys):
    monkeypatch.setenv("DIGEST_EMAIL_TO", "a@example.com")

    def explode(*_args, **_kwargs):
        raise AssertionError("a dry run must not send")

    monkeypatch.setattr(digest, "send", explode)
    result = digest.run(today=date(2026, 9, 20), dry_run=True)

    assert result.status == "skipped"
    assert "Car MOT" in capsys.readouterr().out


def test_run_logs_a_line_on_every_run(stored, monkeypatch, caplog):
    """The line that proves the schedule fired."""
    monkeypatch.setattr(digest, "send", lambda *_a, **_k: mailer.MailResult(status="skipped"))
    with caplog.at_level("INFO", logger="finances.digest"):
        digest.run(today=date(2026, 9, 20))

    messages = [record.message for record in caplog.records]
    assert any("digest run for 2026-09-20" in m for m in messages)
    assert any("email skipped" in m for m in messages)


@pytest.mark.parametrize("status", ["sent", "skipped", "failed"])
def test_run_never_raises(stored, monkeypatch, status):
    monkeypatch.setattr(digest, "send", lambda *_a, **_k: mailer.MailResult(status=status))
    assert digest.run(today=date(2026, 9, 20)).status == status
