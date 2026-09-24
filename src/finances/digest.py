"""The monthly reminder digest: what the calendar reminders used to do.

Composed from exactly the same `calc.attention()` the dashboard renders, so the
email can never tell a different story from the app it links to.

**This logs a line on every run, including a quiet one.** The shared platform's
alerting can see a job that crashed but not one whose schedule silently never
fired — `azure-container-apps`' README names that as its known gap, and the only
cover for it is a run that always reports something. So "nothing is due" is an
outcome worth printing, not a reason to exit early.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from . import calc, store
from .mailer import MailResult, send
from .model import Document
from .money import format_money

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Digest:
    subject: str
    text: str
    html: str
    item_count: int


def compose(doc: Document, today: date, app_url: str = "") -> Digest:
    """Build the email. Pure — takes a document, returns text."""
    state = calc.attention(doc, today)
    totals = calc.summary(doc)

    if state.items:
        subject = f"Finances — {len(state.items)} thing(s) need attention"
    else:
        subject = "Finances — nothing due"

    lines = [f"As of {today:%A %-d %B %Y}.", ""]
    if state.items:
        lines.append("Needs attention")
        lines.append("---------------")
        for item in state.items:
            marker = "!" if item.urgent else "-"
            detail = f" ({item.detail})" if item.detail else ""
            lines.append(f" {marker} {item.title}{detail}")
    else:
        # The quiet path still says what was checked, so an empty digest is
        # distinguishable from a broken one.
        lines.append("Nothing needs attention.")
        lines.append(f"Checked {len(doc.renewals)} renewal(s) and {len(doc.pots)} pot(s).")
    lines.append("")

    lines.append("This month")
    lines.append("----------")
    lines.append(f"  In        {format_money(totals.income)}")
    lines.append(f"  Out       {format_money(totals.outgoings)}")
    lines.append(f"  Savings   {format_money(totals.savings_and_spends)}")
    lines.append(f"  Spare     {format_money(totals.spare)}")
    lines.append("")
    lines.append(f"Pots hold {format_money(calc.pots_total(doc))} across {len(doc.pots)} pot(s).")
    if app_url:
        lines.append("")
        lines.append(app_url)

    text = "\n".join(lines)
    return Digest(
        subject=subject,
        text=text,
        html=_html(state, totals, doc, today, app_url),
        item_count=len(state.items),
    )


def _html(
    state: calc.Attention,
    totals: calc.Summary,
    doc: Document,
    today: date,
    app_url: str,
) -> str:
    """The same content as HTML.

    Inline styles and a table layout, not because it is nice but because that is
    what mail clients render consistently. No external stylesheet, no web font,
    nothing that needs loading — this is read on a phone, often offline.
    """
    from html import escape

    rows = []
    for item in state.items:
        colour = "#b3261e" if item.urgent else "#7a5b00"
        detail = f'<span style="color:#666">{escape(item.detail)}</span>' if item.detail else ""
        rows.append(
            f'<tr><td style="padding:6px 0;border-bottom:1px solid #eee">'
            f'<strong style="color:{colour}">{escape(item.title)}</strong><br>{detail}'
            f"</td></tr>"
        )

    if rows:
        body = f'<table style="width:100%;border-collapse:collapse">{"".join(rows)}</table>'
    else:
        body = (
            '<p style="color:#2e7d32">Nothing needs attention. Checked '
            f"{len(doc.renewals)} renewal(s) and {len(doc.pots)} pot(s).</p>"
        )

    summary_rows = "".join(
        f'<tr><td style="padding:2px 12px 2px 0;color:#666">{label}</td>'
        f'<td style="padding:2px 0;text-align:right"><strong>{value}</strong></td></tr>'
        for label, value in (
            ("In", format_money(totals.income)),
            ("Out", format_money(totals.outgoings)),
            ("Savings", format_money(totals.savings_and_spends)),
            ("Spare", format_money(totals.spare)),
            ("Pots hold", format_money(calc.pots_total(doc))),
        )
    )

    link = (
        f'<p><a href="{escape(app_url)}" style="color:#1a73e8">Open the app</a></p>'
        if app_url
        else ""
    )

    return (
        '<div style="font-family:system-ui,-apple-system,Segoe UI,sans-serif;'
        'font-size:15px;line-height:1.5;color:#1a1a1a;max-width:520px">'
        f'<p style="color:#666">As of {today:%A %-d %B %Y}.</p>'
        f"{body}"
        f'<h3 style="margin-top:24px;font-size:15px">This month</h3>'
        f'<table style="border-collapse:collapse">{summary_rows}</table>'
        f"{link}"
        "</div>"
    )


def run(today: date | None = None, dry_run: bool = False, app_url: str = "") -> MailResult:
    """Read the document, compose, send. What the scheduled job does.

    Returns the mail result so the caller can set an exit code, but a `failed`
    send is not itself a failure — see mailer.py.
    """
    today = today or date.today()
    doc, _etag = store.load()
    digest = compose(doc, today, app_url=app_url)

    if dry_run:
        print(digest.subject)
        print()
        print(digest.text)
        return MailResult(status="skipped")

    result = send(digest.subject, digest.html, digest.text)

    # The line that proves the schedule fired. Every field on it is something
    # the alerting cannot otherwise establish.
    logger.info(
        "digest run for %s: %d item(s) needing attention, email %s",
        today.isoformat(),
        digest.item_count,
        result.status,
    )
    return result
