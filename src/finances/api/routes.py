"""Every page the application serves.

Server-rendered Jinja2. **Every `POST` redirects rather than renders**, so a
reload after recording something does not offer to submit it again — which on a
ledger means a duplicate deposit rather than a harmless no-op.

Writes go through `store.update()`, which reads, applies the change and writes
under an `If-Match` on the ETag. The callables passed to it must be pure: on a
conflict they are re-run against the newer document, so anything generating an
id or reading the clock must do so inside the callable, from the document it is
handed.
"""

from __future__ import annotations

import logging
import pathlib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from fastapi import APIRouter, Form, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import calc, store
from ..model import (
    Document,
    Line,
    PaydayRun,
    Pot,
    PotEntry,
    Renewal,
    Transfer,
    WealthAccount,
    WealthSnapshot,
)
from ..money import format_money, format_pounds, parse_money
from ..settings import settings
from . import deps

logger = logging.getLogger(__name__)

PACKAGE_DIR = pathlib.Path(__file__).resolve().parent.parent
TEMPLATE_DIR = PACKAGE_DIR / "templates"

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
# Used by every template for money, so that one decision about formatting
# applies everywhere rather than being re-made per template.
templates.env.filters["money"] = format_money
templates.env.filters["pounds"] = format_pounds

public = APIRouter()
router = APIRouter()


class ConflictResponse(Exception):
    """Raised when a write lost its race twice. Handled in main.create_app."""


def _today() -> date:
    return datetime.now(UTC).date()


def _back(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=status.HTTP_303_SEE_OTHER)


def _date(value: str, default: date) -> date:
    """A date from a form field, falling back rather than 500-ing.

    `<input type="date">` submits an empty string when left blank, and a browser
    that does not support it submits free text.
    """
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return default


def _apply(change: Any) -> None:
    """Run a document mutation, turning a lost race into a handled error."""
    try:
        store.update(change)
    except store.ConflictError as exc:
        raise ConflictResponse(str(exc)) from exc


@dataclass(frozen=True)
class Sparkline:
    """A valuation trend as SVG coordinates: a `<polyline>`'s points, and the
    same line closed at the baseline for an `<polygon>` area fill under it."""

    line: str
    area: str


def _sparkline(
    snapshots: list[WealthSnapshot], width: float = 160, height: float = 40, pad: float = 4
) -> Sparkline | None:
    """A valuation trend, oldest to newest.

    None with fewer than two valued readings: a single point has no trend to
    draw, and `snapshots_for` includes rows recorded for the projection alone,
    with no current figure at all.
    """
    valued = sorted((s for s in snapshots if s.current_pence is not None), key=lambda s: s.as_of)
    if len(valued) < 2:
        return None
    values = [s.current_pence for s in valued]
    low, high = min(values), max(values)
    span = high - low or 1  # a flat line: centred rather than a division by zero
    plot_height = height - 2 * pad
    step = width / (len(values) - 1)
    line = " ".join(
        f"{i * step:.1f},{height - pad - (v - low) / span * plot_height:.1f}"
        for i, v in enumerate(values)
    )
    area = f"0,{height:.1f} {line} {width:.1f},{height:.1f}"
    return Sparkline(line=line, area=area)


# --- login --------------------------------------------------------------------


@public.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_form(request: Request) -> Any:
    return templates.TemplateResponse(request, "login.html", {"error": ""})


@public.post("/login", include_in_schema=False)
def login(request: Request, passcode: str = Form(default="")) -> Any:
    if not deps.check_passcode(passcode):
        # No detail about *why*. "Wrong passcode" and "no passcode configured"
        # are the same message to whoever is typing.
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "That is not it."},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    response = _back("/")
    response.set_cookie(
        deps.COOKIE_NAME,
        deps.issue(),
        max_age=settings().cookie_max_age_seconds,
        httponly=True,
        samesite="lax",
        # The platform terminates TLS and there is no plain-HTTP route in, so
        # this costs nothing and stops the cookie leaking if that ever changes.
        secure=True,
    )
    return response


@router.post("/logout", include_in_schema=False)
def logout() -> Any:
    response = _back("/login")
    response.delete_cookie(deps.COOKIE_NAME)
    return response


# The icon, drawn here rather than served from a file: there is no static mount
# and no build step, and one SVG covers every size a phone asks for. The glyph
# sits well inside the middle 80% of the canvas, so it survives being masked to
# a circle on Android — which is what `purpose: maskable` below promises.
ICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
    '<rect width="512" height="512" rx="112" fill="#101014"/>'
    '<text x="256" y="256" text-anchor="middle" dominant-baseline="central"'
    ' fill="#7dd3a0" font-size="300" font-weight="600"'
    ' font-family="system-ui,-apple-system,Segoe UI,Helvetica,Arial,sans-serif">'
    "\u00a3</text></svg>"
)

# A week: the icon changes about never, and the phone re-requests it on every
# cold start of an app opened twice a month.
ICON_HEADERS = {"Cache-Control": "public, max-age=604800"}


def _icon_response() -> Response:
    return Response(content=ICON_SVG, media_type="image/svg+xml", headers=ICON_HEADERS)


@public.get("/icon.svg", include_in_schema=False)
def icon() -> Response:
    """The app icon: favicon, home-screen tile and manifest icon, all one file.

    Public, like the manifest: an icon behind the passcode gate would resolve to
    a redirect to /login on the one page that most needs it, which is the login
    page itself.
    """
    return _icon_response()


@public.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    """Browsers ask for this by name regardless of what the document links to.

    Served rather than left to 404 because it was the only 404 a normal page
    load produced, which makes it the thing anyone reads the logs for. The bytes
    are the SVG above, and the content type rather than the extension is what
    every browser goes on.
    """
    return _icon_response()


@public.get("/manifest.json", include_in_schema=False)
def manifest() -> JSONResponse:
    """What lets the phone install this to the home screen.

    `display: standalone` is the point of it — opened from the home screen there
    is no browser chrome, which on a phone is a whole row of pot balances.
    """
    return JSONResponse(
        {
            "name": "finances",
            "short_name": "money",
            "start_url": "/",
            "display": "standalone",
            "background_color": "#101014",
            "theme_color": "#101014",
            "icons": [
                {
                    "src": "/icon.svg",
                    "type": "image/svg+xml",
                    # "any": an SVG has no one size, and stating a fixed pair
                    # here is what stops Chrome offering to install it.
                    "sizes": "any",
                    "purpose": "any maskable",
                }
            ],
        }
    )


# --- dashboard ----------------------------------------------------------------


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def index(request: Request) -> Any:
    doc, _etag = store.load()
    today = _today()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "doc": doc,
            "today": today,
            "attention": calc.attention(doc, today),
            "pots_total": calc.pots_total(doc),
            "wealth_total": calc.wealth_total(doc),
        },
    )


# --- payday -------------------------------------------------------------------


@router.get("/payday", response_class=HTMLResponse, include_in_schema=False)
def payday_form(request: Request) -> Any:
    doc, _etag = store.load()
    today = _today()
    month = calc.current_month(today)
    return templates.TemplateResponse(
        request,
        "payday.html",
        {
            "doc": doc,
            "today": today,
            "month": month,
            "pots": calc.payday_pots(doc),
            "balances": calc.pot_balances(doc),
            "existing": doc.payday_run_for(month),
            "monthly_total": calc.pots_monthly_total(doc),
            "transfers": [(t, calc.transfer_amount(doc, t.id)) for t in doc.transfers],
            "remainder": calc.summary(doc).spare,
        },
    )


@router.post("/payday/run", include_in_schema=False)
async def payday_run(request: Request) -> Any:
    """Top up every pot, in one write.

    The amounts come back from the form rather than being read from the pots
    again, so an unusual month — skipping Holidays, or putting in double — is
    recorded as what actually happened rather than as the default.
    """
    form = await request.form()
    on = _date(str(form.get("on", "")), _today())
    month = calc.current_month(on)

    def change(doc: Document) -> Document:
        entry_ids: list[str] = []
        for pot in calc.payday_pots(doc):
            raw = str(form.get(f"amount_{pot.id}", "")).strip()
            if not raw:
                continue  # unticked or cleared: this pot sits this month out
            amount = parse_money(raw)
            if amount == 0:
                continue
            entry = PotEntry(
                pot_id=pot.id,
                on=on,
                amount_pence=amount,
                kind="payday",
                note=f"Payday {month}",
            )
            doc.pot_entries.append(entry)
            entry_ids.append(entry.id)

        # Replace rather than append if this month was already run: re-running
        # is how a mistake gets corrected, and two runs for one month would
        # double every balance while looking like history.
        existing = doc.payday_run_for(month)
        if existing is not None:
            doc.pot_entries = [e for e in doc.pot_entries if e.id not in set(existing.entry_ids)]
            doc.payday_runs = [r for r in doc.payday_runs if r.id != existing.id]

        doc.payday_runs.append(PaydayRun(month=month, run_on=on, entry_ids=entry_ids))
        return doc

    _apply(change)
    logger.info("payday run recorded for %s", month)
    return _back("/pots")


@router.post("/payday/transfers", include_in_schema=False)
async def payday_transfers_save(request: Request) -> Any:
    """Save every editable transfer amount in one write.

    The `tracks_pots` transfer and the remainder step have nothing here to
    save — the first is derived from the pots, the second is whatever is left.
    """
    form = await request.form()

    def change(doc: Document) -> Document:
        for transfer in doc.transfers:
            raw = form.get(f"transfer_{transfer.id}")
            if raw is not None and not transfer.tracks_pots:
                text = str(raw).strip()
                transfer.amount_pence = parse_money(text) if text else None
        return doc

    _apply(change)
    return _back("/payday")


@router.post("/payday/transfers/add", include_in_schema=False)
def payday_transfer_add(
    label: str = Form(...), amount: str = Form(default=""), note: str = Form(default="")
) -> Any:
    def change(doc: Document) -> Document:
        text = amount.strip()
        doc.transfers.append(
            Transfer(
                label=label.strip(),
                amount_pence=parse_money(text) if text else None,
                note=note.strip(),
            )
        )
        return doc

    _apply(change)
    return _back("/payday")


@router.post("/payday/transfers/{transfer_id}/delete", include_in_schema=False)
def payday_transfer_delete(transfer_id: str) -> Any:
    def change(doc: Document) -> Document:
        doc.transfers = [t for t in doc.transfers if t.id != transfer_id]
        return doc

    _apply(change)
    return _back("/payday")


# --- pots ---------------------------------------------------------------------


@router.get("/pots", response_class=HTMLResponse, include_in_schema=False)
def pots_page(request: Request) -> Any:
    doc, _etag = store.load()
    today = _today()
    return templates.TemplateResponse(
        request,
        "pots.html",
        {
            "doc": doc,
            "today": today,
            "balances": calc.pot_balances(doc),
            "pots_total": calc.pots_total(doc),
            "monthly_total": calc.pots_monthly_total(doc),
        },
    )


@router.get("/pots/{pot_id}", response_class=HTMLResponse, include_in_schema=False)
def pot_page(request: Request, pot_id: str) -> Any:
    doc, _etag = store.load()
    pot = doc.pot(pot_id)
    if pot is None:
        return _back("/pots")
    return templates.TemplateResponse(
        request,
        "pot.html",
        {
            "doc": doc,
            "pot": pot,
            "today": _today(),
            "balance": calc.pot_balance(doc, pot_id),
            "entries": doc.entries_for(pot_id),
        },
    )


@router.post("/pots/add", include_in_schema=False)
def pot_add(name: str = Form(...), monthly: str = Form(default="")) -> Any:
    def change(doc: Document) -> Document:
        doc.pots.append(Pot(name=name.strip(), monthly_pence=parse_money(monthly)))
        return doc

    _apply(change)
    return _back("/pots")


@router.post("/pots/{pot_id}/edit", include_in_schema=False)
def pot_edit(
    pot_id: str,
    name: str = Form(...),
    monthly: str = Form(default=""),
    counts: str = Form(default=""),
    archived: str = Form(default=""),
) -> Any:
    def change(doc: Document) -> Document:
        pot = doc.pot(pot_id)
        if pot is not None:
            pot.name = name.strip()
            pot.monthly_pence = parse_money(monthly)
            pot.counts_toward_payday = bool(counts)
            pot.archived = bool(archived)
        return doc

    _apply(change)
    return _back(f"/pots/{pot_id}")


@router.post("/pots/{pot_id}/entry", include_in_schema=False)
def pot_entry(
    pot_id: str,
    amount: str = Form(...),
    kind: str = Form(default="deposit"),
    on: str = Form(default=""),
    note: str = Form(default=""),
) -> Any:
    """Record a deposit or a spend.

    A spend is stored as a negative amount whatever sign was typed, so "100"
    and "-100" in the spend box both mean the same thing. Getting that wrong
    silently doubles a pot instead of halving it.
    """
    value = abs(parse_money(amount))
    signed = -value if kind == "spend" else value
    when = _date(on, _today())

    def change(doc: Document) -> Document:
        if doc.pot(pot_id) is not None:
            doc.pot_entries.append(
                PotEntry(
                    pot_id=pot_id,
                    on=when,
                    amount_pence=signed,
                    kind="spend" if kind == "spend" else "deposit",
                    note=note.strip(),
                )
            )
        return doc

    _apply(change)
    return _back(f"/pots/{pot_id}")


@router.post("/pots/{pot_id}/entry/{entry_id}/delete", include_in_schema=False)
def pot_entry_delete(pot_id: str, entry_id: str) -> Any:
    def change(doc: Document) -> Document:
        doc.pot_entries = [e for e in doc.pot_entries if e.id != entry_id]
        # Any payday run that created it stops claiming it, so re-running that
        # month does not try to remove an entry that is already gone.
        for run in doc.payday_runs:
            if entry_id in run.entry_ids:
                run.entry_ids = [i for i in run.entry_ids if i != entry_id]
        return doc

    _apply(change)
    return _back(f"/pots/{pot_id}")


# --- the monthly picture ------------------------------------------------------


@router.get("/monthly", response_class=HTMLResponse, include_in_schema=False)
def monthly_page(request: Request) -> Any:
    doc, _etag = store.load()
    return templates.TemplateResponse(
        request,
        "monthly.html",
        {
            "doc": doc,
            "today": _today(),
            "summary": calc.summary(doc),
        },
    )


@router.post("/monthly", include_in_schema=False)
async def monthly_save(request: Request) -> Any:
    """Save every amount on the page in one write.

    A field per line rather than a page per line: changing a bill is usually
    done in a batch when the statement arrives, and eleven round trips to edit
    eleven amounts is what sends someone back to the spreadsheet.
    """
    form = await request.form()

    def change(doc: Document) -> Document:
        for line in [*doc.income, *doc.outgoings]:
            raw = form.get(f"amount_{line.id}")
            if raw is not None:
                line.amount_pence = parse_money(str(raw))
            name = form.get(f"name_{line.id}")
            if name is not None and str(name).strip():
                line.name = str(name).strip()
            # An unchecked checkbox submits nothing at all, so presence is the
            # value. Only lines the form actually rendered are touched.
            if form.get(f"present_{line.id}") is not None:
                line.active = form.get(f"active_{line.id}") is not None
        return doc

    _apply(change)
    return _back("/monthly")


@router.post("/monthly/add", include_in_schema=False)
def monthly_add(
    kind: str = Form(...), name: str = Form(...), amount: str = Form(default="")
) -> Any:
    def change(doc: Document) -> Document:
        line = Line(name=name.strip(), amount_pence=parse_money(amount))
        if kind == "income":
            doc.income.append(line)
        else:
            doc.outgoings.append(line)
        return doc

    _apply(change)
    return _back("/monthly")


@router.post("/monthly/{line_id}/delete", include_in_schema=False)
def monthly_delete(line_id: str) -> Any:
    def change(doc: Document) -> Document:
        doc.income = [line for line in doc.income if line.id != line_id]
        doc.outgoings = [line for line in doc.outgoings if line.id != line_id]
        return doc

    _apply(change)
    return _back("/monthly")


# --- renewals -----------------------------------------------------------------


@router.get("/renewals", response_class=HTMLResponse, include_in_schema=False)
def renewals_page(request: Request) -> Any:
    doc, _etag = store.load()
    today = _today()
    renewals = calc.renewals_by_date(doc, today)
    return templates.TemplateResponse(
        request,
        "renewals.html",
        {
            "doc": doc,
            "today": today,
            "renewals": [(r, calc.days_until(r, today), calc.is_due(r, today)) for r in renewals],
        },
    )


@router.post("/renewals/add", include_in_schema=False)
def renewal_add(
    kind: str = Form(...),
    company: str = Form(default=""),
    expires_on: str = Form(default=""),
    rolling: str = Form(default=""),
    comment: str = Form(default=""),
) -> Any:
    def change(doc: Document) -> Document:
        doc.renewals.append(
            Renewal(
                kind=kind.strip(),
                company=company.strip(),
                expires_on=None if rolling or not expires_on else _date(expires_on, _today()),
                rolling=bool(rolling),
                comment=comment.strip(),
            )
        )
        return doc

    _apply(change)
    return _back("/renewals")


@router.post("/renewals/{renewal_id}", include_in_schema=False)
def renewal_edit(
    renewal_id: str,
    kind: str = Form(...),
    company: str = Form(default=""),
    expires_on: str = Form(default=""),
    rolling: str = Form(default=""),
    notice_days: str = Form(default=""),
    comment: str = Form(default=""),
) -> Any:
    def change(doc: Document) -> Document:
        for renewal in doc.renewals:
            if renewal.id != renewal_id:
                continue
            renewal.kind = kind.strip()
            renewal.company = company.strip()
            renewal.rolling = bool(rolling)
            renewal.expires_on = (
                None if renewal.rolling or not expires_on else _date(expires_on, _today())
            )
            renewal.notice_days = int(notice_days) if notice_days.strip().isdigit() else None
            renewal.comment = comment.strip()
        return doc

    _apply(change)
    return _back("/renewals")


@router.post("/renewals/{renewal_id}/delete", include_in_schema=False)
def renewal_delete(renewal_id: str) -> Any:
    def change(doc: Document) -> Document:
        doc.renewals = [r for r in doc.renewals if r.id != renewal_id]
        return doc

    _apply(change)
    return _back("/renewals")


# --- wealth -------------------------------------------------------------------


def _parse_rate(value: str) -> float | None:
    """A form percentage like `"5"` or `"-2.5"` to the ratio `calc` expects.

    Blank or unparseable is None rather than raising — same latitude
    `target_retirement_year` gets below, since this is an optional override,
    not a required figure.
    """
    if not value.strip():
        return None
    try:
        return float(value) / 100
    except ValueError:
        return None


@router.get("/wealth", response_class=HTMLResponse, include_in_schema=False)
def wealth_page(request: Request) -> Any:
    doc, _etag = store.load()
    today = _today()
    accounts = []
    for account in doc.wealth_accounts:
        history = doc.snapshots_for(account.id)
        latest = doc.latest_snapshot(account.id)
        age = (today - latest.as_of).days if latest else None
        estimate = calc.projected_retirement_value(account, latest, today)
        change = calc.valuation_change(history)
        accounts.append((account, latest, age, history, _sparkline(history), estimate, change))
    # Still-contributing first: the one pension actually growing by choice
    # rather than by market luck is the one worth seeing without scrolling.
    accounts.sort(key=lambda row: not row[0].still_contributing)
    return templates.TemplateResponse(
        request,
        "wealth.html",
        {
            "doc": doc,
            "today": today,
            "accounts": accounts,
            "total": calc.wealth_total(doc),
            "projection": calc.projection_total(doc),
            "retirement_estimate": calc.retirement_estimate_total(doc, today),
            "stale_after": settings().wealth_stale_days,
        },
    )


@router.post("/wealth/accounts/add", include_in_schema=False)
def wealth_account_add(
    company: str = Form(...),
    notes: str = Form(default=""),
    still_contributing: str = Form(default=""),
) -> Any:
    def change(doc: Document) -> Document:
        doc.wealth_accounts.append(
            WealthAccount(
                company=company.strip(),
                notes=notes.strip(),
                still_contributing=bool(still_contributing),
            )
        )
        return doc

    _apply(change)
    return _back("/wealth")


@router.post("/wealth/accounts/{account_id}", include_in_schema=False)
def wealth_account_edit(
    account_id: str,
    company: str = Form(...),
    notes: str = Form(default=""),
    still_contributing: str = Form(default=""),
    target_retirement_year: str = Form(default=""),
    assumed_growth_rate: str = Form(default=""),
) -> Any:
    def change(doc: Document) -> Document:
        account = doc.account(account_id)
        if account is None or not account.editable:
            return doc
        account.company = company.strip()
        account.notes = notes.strip()
        account.still_contributing = bool(still_contributing)
        account.target_retirement_year = (
            int(target_retirement_year) if target_retirement_year.strip().isdigit() else None
        )
        account.assumed_growth_rate = _parse_rate(assumed_growth_rate)
        return doc

    _apply(change)
    return _back("/wealth")


@router.post("/wealth/{account_id}/snapshot", include_in_schema=False)
def wealth_snapshot_add(
    account_id: str,
    as_of: str = Form(default=""),
    current: str = Form(default=""),
    projection: str = Form(default=""),
) -> Any:
    """Record this quarter's valuation.

    Appended, never overwritten: the whole reason for the quarterly ritual is
    the trend, and the spreadsheet threw away every previous reading.

    Year growth is no longer typed in: it is worked out from this figure
    against the account's history, inside `change` so a retry after a lost
    write compares against the same history the first attempt did.
    """
    when = _date(as_of, _today())

    def change(doc: Document) -> Document:
        if doc.account(account_id) is None:
            return doc
        current_pence = parse_money(current) if current.strip() else None
        history = doc.snapshots_for(account_id)
        doc.wealth_snapshots.append(
            WealthSnapshot(
                account_id=account_id,
                as_of=when,
                current_pence=current_pence,
                yearly_projection_pence=parse_money(projection) if projection.strip() else None,
                year_growth=(
                    calc.annualised_growth(history, current_pence, when)
                    if current_pence is not None
                    else None
                ),
            )
        )
        return doc

    _apply(change)
    return _back("/wealth")


@router.post("/wealth/{account_id}/snapshot/{snapshot_id}/delete", include_in_schema=False)
def wealth_snapshot_delete(account_id: str, snapshot_id: str) -> Any:
    """Correct a mistyped valuation.

    Matched on both ids rather than just the snapshot's, so a stale form from
    a since-deleted account cannot delete a reading that has since been
    reassigned. Deliberately does not recompute any later snapshot's stored
    `year_growth` — those were worked out against whatever the previous
    reading was at the time, same as the rest of this app's figures.
    """

    def change(doc: Document) -> Document:
        doc.wealth_snapshots = [
            s
            for s in doc.wealth_snapshots
            if not (s.id == snapshot_id and s.account_id == account_id)
        ]
        return doc

    _apply(change)
    return _back("/wealth")


@router.post("/wealth/accounts/{account_id}/delete", include_in_schema=False)
def wealth_account_delete(account_id: str) -> Any:
    def change(doc: Document) -> Document:
        account = doc.account(account_id)
        if account is None or not account.editable:
            return doc
        doc.wealth_accounts = [a for a in doc.wealth_accounts if a.id != account_id]
        # Its snapshots go with it: an orphaned snapshot is invisible in the UI
        # but still counted by nothing, which is worse than being gone.
        doc.wealth_snapshots = [s for s in doc.wealth_snapshots if s.account_id != account_id]
        return doc

    _apply(change)
    return _back("/wealth")
