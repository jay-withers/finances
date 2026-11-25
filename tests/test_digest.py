"""The scheduled reminder digest."""

from __future__ import annotations

from datetime import date

import pytest

from finances import digest, mailer
from finances.model import Document, Line, PaydayRun, Pot, PotEntry, Renewal, Transfer


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


def test_app_url_links_to_the_today_page(doc: Document, today: date):
    """The email is the Today page's list; the root is the home tiles."""
    for url in ("https://finances.example.uk", "https://finances.example.uk/"):
        composed = digest.compose(doc, today, app_url=url)
        assert "https://finances.example.uk/today" in composed.text
        assert 'href="https://finances.example.uk/today"' in composed.html


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


def test_send_with_no_client_uses_httpx_directly(monkeypatch):
    """The default path (no test double): `mailer.send` calls `httpx.post` itself."""
    import httpx

    monkeypatch.setenv("DIGEST_EMAIL_TO", "a@example.com")
    monkeypatch.setenv("RESEND_API_KEY", "key-123")

    calls = []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append((url, json, headers, timeout))
        return FakeResponse()

    monkeypatch.setattr(httpx, "post", fake_post)

    result = mailer.send("subject", "<p>html</p>", "text")

    assert result.status == "sent"
    url, _payload, _headers, timeout = calls[0]
    assert url == mailer.RESEND_ENDPOINT
    assert timeout == mailer.TIMEOUT_SECONDS


def test_a_failed_send_is_reported_not_raised(monkeypatch):
    """A mail provider's bad minute must not become a job-failure alert."""
    monkeypatch.setenv("DIGEST_EMAIL_TO", "a@example.com")
    monkeypatch.setenv("RESEND_API_KEY", "key-123")
    client = FakeClient(FakeResponse(error=RuntimeError("502 Bad Gateway")))

    result = mailer.send("subject", "<p>html</p>", "text", client=client)

    assert result.status == "failed"
    assert result.error is not None
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


def test_digest_lists_the_last_7_days_of_movements(today: date):
    document = Document()
    document.pots = [Pot(id="a", name="Car <b>")]
    document.pot_entries = [
        PotEntry(pot_id="a", on=today, amount_pence=-1000, kind="spend", note="tyres"),
        PotEntry(pot_id="a", on=date(2026, 1, 1), amount_pence=-5, kind="spend", note="ancient"),
    ]

    composed = digest.compose(document, today)

    assert "Last 7 days" in composed.text
    assert "Car <b> · tyres  -£10" in composed.text
    assert "ancient" not in composed.text
    assert "Car &lt;b&gt; · tyres" in composed.html


def test_a_week_with_no_movement_says_so(today: date):
    composed = digest.compose(Document(), today)
    assert "No money moved." in composed.text
    assert "No money moved." in composed.html


def test_digest_says_where_the_income_goes(doc: Document, today: date):
    """The Insights split, each part with the biggest thing in it."""
    composed = digest.compose(doc, today, app_url="https://finances.example.uk")

    text = composed.text
    assert "Where the income goes" in text
    # Columns are padded; compare words.
    section = " ".join(text[text.index("Where the income goes") :].split())
    assert "Bills £1,000 20%" in section
    assert "largest: Mortgage £900" in section
    assert "Savings £500 10%" in section
    assert "largest: Holidays £450" in section
    assert "Spends £3,500 70%" in section
    # What is left over isn't a thing spent on, so it is never the largest.
    assert "largest: Bills account £1,100" in section
    assert "Left over" not in section
    assert "Debt" not in section  # nothing in it

    html = composed.html
    assert "Where the income goes: £5,000 a month" in html
    for share in ("Bills", "Savings", "Spends"):
        assert f'title="{share}"' in html  # a segment of the bar
    assert "width:70%" in html
    assert "largest: Mortgage £900" in html
    assert 'href="https://finances.example.uk/insights"' in html
    assert "https://finances.example.uk/insights" in text


def test_the_income_section_escapes_what_a_person_typed(doc: Document, today: date):
    doc.outgoings[0].name = "<b>Mortgage</b>"
    html = digest.compose(doc, today).html
    assert "<b>Mortgage</b>" not in html
    assert "largest: &lt;b&gt;Mortgage&lt;/b&gt;" in html


def test_the_income_section_names_an_overspend(doc: Document, today: date):
    doc.outgoings[0].amount_pence = 400_000
    composed = digest.compose(doc, today)
    message = "The plan spends £700 a month more than comes in."
    assert message in composed.text
    assert message in composed.html


def test_the_income_section_with_no_income(today: date):
    composed = digest.compose(Document(), today)
    assert "No income recorded." in composed.text
    assert "No income recorded." in composed.html
    assert "£0 a month" not in composed.html
    assert "/insights" not in composed.html  # no app_url, no link


def _bar_widths(html: str) -> list[int]:
    import re

    return [int(w) for w in re.findall(r'<td style="width:(\d+)%;height:12px', html)]


def test_the_income_bar_always_fills_its_width(doc: Document, today: date):
    """Whole percents can round to 99 or 101; the last cell takes the slack,
    and a part that rounds to 0% gets no cell (its row still names it)."""
    doc.income = [Line(name="Salary", amount_pence=300)]
    doc.outgoings = [Line(name="A", amount_pence=100)]
    doc.pots = [Pot(name="B", monthly_pence=100)]
    doc.transfers = [Transfer(label="Savings", amount_pence=None, tracks_pots=True)]
    html = digest.compose(doc, today).html
    assert _bar_widths(html) == [33, 33, 34]

    doc.income = [Line(name="Salary", amount_pence=1_000_000)]
    doc.outgoings = [Line(name="Tiny", amount_pence=100)]  # 0.01%
    html = digest.compose(doc, today).html
    assert sum(_bar_widths(html)) == 100
    assert 'title="Bills"' not in html
    assert "Tiny" in html


def test_the_email_colours_are_the_insights_page_colours():
    """The email draws Insights' bar, so it wears Insights' palette."""
    import re
    from pathlib import Path

    import finances

    template = (Path(finances.__file__).parent / "templates" / "insights.html").read_text()
    css = dict(re.findall(r"--(c-[a-z]+):\s*(#[0-9a-f]{6})", template))
    assert {
        "bills": css["c-out"],
        "savings": css["c-in"],
        "spends": css["c-spends"],
        "debt": css["c-debt"],
    } == digest._PART_COLOURS
