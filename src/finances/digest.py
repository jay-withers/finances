"""The weekly reminder digest: what the calendar reminders used to do.

Composed from exactly the same `calc.attention()` the Today page renders, and
the same `calc.income_split()` as Insights' "where the income goes", so the
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


@dataclass(frozen=True)
class IncomePart:
    """One part of where the income goes, with the biggest thing in it."""

    share: calc.IncomeShare
    largest: calc.PlanItem | None


def _income_parts(doc: Document, totals: calc.Summary) -> list[IncomePart]:
    return [IncomePart(s, calc.largest_in(doc, s.part)) for s in calc.income_split(totals)]


def _income_note(totals: calc.Summary) -> str:
    """What the section says when there is no split to show."""
    if totals.income <= 0:
        return "No income recorded."
    return f"The plan spends {format_money(-totals.spare)} a month more than comes in."


# The Insights bar's colours (insights.html's --c-out, --c-in, --c-spends,
# --c-debt), validated there in this order.
_PART_COLOURS = {"bills": "#c96a2c", "savings": "#4a90d9", "spends": "#4f9e4a", "debt": "#d6578f"}


def _app_page(app_url: str, page: str) -> str:
    return f"{app_url.rstrip('/')}/{page}"


def _today_url(app_url: str) -> str:
    """The app's Today page, which lists exactly what this email's attention
    items are. The root is the home tiles, one tap further from it."""
    return _app_page(app_url, "today")


def compose(doc: Document, today: date, app_url: str = "") -> Digest:
    """Build the email. Pure — takes a document, returns text."""
    state = calc.attention(doc, today)
    totals = calc.summary(doc)
    recent = calc.recent_movements(doc, today)
    income = _income_parts(doc, totals)

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
        lines.append(
            f"Checked {len(doc.renewals)} renewal(s), {len(doc.pots)} pot(s) "
            f"and {len(doc.wealth_accounts)} wealth account(s)."
        )
    lines.append("")

    lines.append("Last 7 days")
    lines.append("-----------")
    if recent:
        for move in recent:
            lines.append(f"  {move.on:%-d %b}  {move.label}  {format_money(move.amount_pence)}")
    else:
        lines.append("  No money moved.")
    lines.append("")

    lines.append("This month")
    lines.append("----------")
    lines.append(f"  In        {format_money(totals.income)}")
    lines.append(f"  Out       {format_money(totals.outgoings)}")
    lines.append(f"  Savings   {format_money(totals.savings_and_spends)}")
    lines.append(f"  Spare     {format_money(totals.spare)}")
    lines.append("")
    lines.append(f"Pots hold {format_money(calc.pots_total(doc))} across {len(doc.pots)} pot(s).")
    lines.append("")

    lines.append("Where the income goes")
    lines.append("---------------------")
    if income:
        for part in income:
            share = part.share
            lines.append(
                f"  {share.label:<9} {format_money(share.amount_pence):>10}  {share.percent:>3}%"
            )
            if part.largest is not None:
                lines.append(
                    f"            largest: {part.largest.label} "
                    f"{format_money(part.largest.amount_pence)}"
                )
    else:
        lines.append(f"  {_income_note(totals)}")
    if app_url and income:
        lines.append(f"  {_app_page(app_url, 'insights')}")
    if app_url:
        lines.append("")
        lines.append(_today_url(app_url))

    text = "\n".join(lines)
    return Digest(
        subject=subject,
        text=text,
        html=_html(state, totals, recent, income, doc, today, app_url),
        item_count=len(state.items),
    )


def _html(
    state: calc.Attention,
    totals: calc.Summary,
    recent: list[calc.Movement],
    income: list[IncomePart],
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
            f"{len(doc.renewals)} renewal(s), {len(doc.pots)} pot(s) and "
            f"{len(doc.wealth_accounts)} wealth account(s).</p>"
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

    recent_rows = (
        "".join(
            f'<tr><td style="padding:2px 12px 2px 0;color:#666">{move.on:%-d %b}</td>'
            f'<td style="padding:2px 12px 2px 0">{escape(move.label)}</td>'
            f'<td style="padding:2px 0;text-align:right;'
            f'color:{"#b3261e" if move.amount_pence < 0 else "#1a1a1a"}">'
            f"{format_money(move.amount_pence)}</td></tr>"
            for move in recent
        )
        or '<tr><td style="color:#666">No money moved.</td></tr>'
    )

    # The bar is table cells, not a flexbox or SVG: the one layout every mail
    # client draws. Colours are the Insights page's, each part also named in
    # the rows beneath, so the bar never carries the meaning alone.
    if income:
        # A part rounding to 0% has no cell, and the last takes what rounding
        # left, so the widths always make 100%.
        drawn = [p for p in income if p.share.percent > 0]
        widths = [p.share.percent for p in drawn]
        widths[-1] = 100 - sum(widths[:-1])
        bar = "".join(
            f'<td style="width:{width}%;height:12px;padding:0;'
            f'background:{_PART_COLOURS[p.share.part]}" title="{p.share.label}"></td>'
            for p, width in zip(drawn, widths, strict=True)
        )
        income_rows = "".join(
            f'<tr><td style="padding:2px 12px 2px 0">'
            f'<span style="display:inline-block;width:10px;height:10px;border-radius:3px;'
            f'margin-right:6px;background:{_PART_COLOURS[p.share.part]}"></span>'
            f"{p.share.label}</td>"
            f'<td style="padding:2px 12px 2px 0;text-align:right">'
            f"<strong>{format_money(p.share.amount_pence)}</strong></td>"
            f'<td style="padding:2px 12px 2px 0;text-align:right;color:#666">'
            f"{p.share.percent}%</td>"
            f'<td style="padding:2px 0;color:#666">'
            + (
                f"largest: {escape(p.largest.label)} {format_money(p.largest.amount_pence)}"
                if p.largest is not None
                else ""
            )
            + "</td></tr>"
            for p in income
        )
        income_html = (
            '<table style="width:100%;border-collapse:separate;border-spacing:2px 0;'
            f'margin:4px 0 8px"><tr>{bar}</tr></table>'
            f'<table style="border-collapse:collapse">{income_rows}</table>'
        )
    else:
        income_html = f'<p style="color:#666">{escape(_income_note(totals))}</p>'
    insights_link = (
        f'<p style="font-size:13px"><a href="{escape(_app_page(app_url, "insights"))}" '
        'style="color:#1a73e8">See it on Insights</a></p>'
        if app_url and income
        else ""
    )
    income_heading = "Where the income goes" + (
        f": {format_money(totals.income)} a month" if income else ""
    )

    link = (
        f'<p><a href="{escape(_today_url(app_url))}" style="color:#1a73e8">Open the app</a></p>'
        if app_url
        else ""
    )

    return (
        '<div style="font-family:system-ui,-apple-system,Segoe UI,sans-serif;'
        'font-size:15px;line-height:1.5;color:#1a1a1a;max-width:520px">'
        f'<p style="color:#666">As of {today:%A %-d %B %Y}.</p>'
        f"{body}"
        f'<h3 style="margin-top:24px;font-size:15px">Last 7 days</h3>'
        f'<table style="border-collapse:collapse">{recent_rows}</table>'
        f'<h3 style="margin-top:24px;font-size:15px">This month</h3>'
        f'<table style="border-collapse:collapse">{summary_rows}</table>'
        f'<h3 style="margin-top:24px;font-size:15px">{income_heading}</h3>'
        f"{income_html}"
        f"{insights_link}"
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
