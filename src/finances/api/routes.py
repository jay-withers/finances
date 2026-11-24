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
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from appkit import csrf
from appkit import manifest as app_manifest
from fastapi import APIRouter, Form, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import calc, charts, store
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
templates.env.globals["csrf_token"] = csrf.token
# Used by every template for money, so that one decision about formatting
# applies everywhere rather than being re-made per template.
templates.env.filters["money"] = format_money
templates.env.filters["pounds"] = format_pounds

public = APIRouter()
router = APIRouter()


class ConflictResponse(Exception):
    """Raised when a write lost its race twice. Handled in main.create_app."""


class InvalidInput(Exception):
    """A form value that would record something wrong. Handled in
    main.create_app, which answers with `status_code` and the message, so
    whoever typed it sees it was not recorded."""

    def __init__(self, message: str, status_code: int = status.HTTP_422_UNPROCESSABLE_CONTENT):
        super().__init__(message)
        self.status_code = status_code


def _amount(raw: str) -> int:
    """`parse_money`, with a typo answered as bad input rather than a 500."""
    try:
        return parse_money(raw)
    except ValueError as exc:
        raise InvalidInput("That amount isn't a number.") from exc


def _purpose(raw: str) -> tuple[bool, bool]:
    """A pot's purpose from its form, as the (debt, bills) flags it stores."""
    if raw not in ("savings", "bills", "debt"):
        raise InvalidInput("A pot is for savings, bills or a debt.")
    return raw == "debt", raw == "bills"


def _today() -> date:
    return datetime.now(UTC).date()


# A `YYYY-MM` month key, as the ledger's month blocks are named.
_MONTH = re.compile(r"\d{4}-\d{2}")


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
    values = [pence for _, pence in calc.valued_readings(snapshots)]
    if len(values) < 2:
        return None
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


def _csrf_refusal(request: Request) -> str:
    """Why the login form was refused. No cookie at all means the browser won't
    keep it (cookies blocked, or plain http on a host other than localhost,
    where a `Secure` cookie is dropped), and "try again" would fail the same way."""
    if request.cookies.get(csrf.COOKIE_NAME):
        return "That form had expired. Try again."
    return "Signing in needs cookies, over https or on localhost."


@public.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_form(request: Request) -> Any:
    return templates.TemplateResponse(request, "login.html", {"error": ""})


@public.post("/login", include_in_schema=False)
def login(
    request: Request, passcode: str = Form(default=""), csrf_token: str = Form(default="")
) -> Any:
    # Before the passcode, so a forged post can't even try one. The re-render
    # carries a fresh token, so a retry works (appkit.csrf: two tabs, one cookie).
    if not csrf.valid(request, csrf_token):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": _csrf_refusal(request)},
            status_code=status.HTTP_403_FORBIDDEN,
        )
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


# The favicon, drawn here rather than served from a file: one SVG covers every
# size a browser tab asks for. The home-screen icons are the PNGs below.
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


# The Android home-screen icons, rendered from the same design by
# scripts/render-icons.py and committed. PNG because Android Chrome builds the
# installed app from a raster icon. The maskable one is full-bleed with the
# glyph inside the middle 80%, because the launcher crops it to its own shape.
# Read once at import: three small files, served on every cold start.
ICON_DIR = PACKAGE_DIR / "icons"
# appkit names the icons, which the manifest and the route below share.
PNG_BYTES = {name: (ICON_DIR / name).read_bytes() for name, _, _ in app_manifest.PNG_ICONS}


@public.get("/icons/{name}", include_in_schema=False)
def png_icon(name: str) -> Response:
    """A home-screen icon. Public, for the same reason as /icon.svg."""
    content = PNG_BYTES.get(name)
    if content is None:
        return Response(status_code=status.HTTP_404_NOT_FOUND)
    return Response(content=content, media_type="image/png", headers=ICON_HEADERS)


@public.get("/manifest.json", include_in_schema=False)
def manifest() -> JSONResponse:
    """What lets the phone install this to the home screen, built by appkit:
    opened on the home page, with `id` pinned to `/`.

    `display: standalone` is the point of it — opened from the home screen there
    is no browser chrome, which on a phone is a whole row of pot balances.
    """
    return JSONResponse(
        app_manifest.build(
            name="finances",
            short_name="money",
            colour="#101014",
            icons=app_manifest.png_icons("/icons"),
        )
    )


# --- home and today -----------------------------------------------------------


@dataclass(frozen=True)
class Tile:
    """One area on the home screen: where it goes, and the one figure or
    sentence that says whether it needs opening."""

    title: str
    href: str
    value: str
    detail: str = ""
    # "" for nothing to see, "warn" for something to do, "urgent" for
    # something overdue. Drives the tile's edge colour, beside its words.
    state: str = ""


def _tiles(doc: Document, today: date) -> list[Tile]:
    attention = calc.attention(doc, today)
    recent = calc.recent_movements(doc, today)
    if attention.items:
        first = attention.items[0]
        today_tile = Tile(
            "Today",
            "/today",
            f"{calc.plural(len(attention.items), 'thing')} to look at",
            first.title
            if len(attention.items) == 1
            else f"{first.title}, and {len(attention.items) - 1} more",
            "urgent" if any(i.urgent for i in attention.items) else "warn",
        )
    else:
        today_tile = Tile(
            "Today",
            "/today",
            "All clear",
            f"{calc.plural(len(recent), 'movement')} in the last 7 days",
        )

    month = calc.current_month(today)
    payday = calc.household_payday(today)
    run = doc.payday_run_for(month)
    if not calc.payday_pots(doc):
        payday_tile = Tile("Payday", "/payday", "No pots to top up")
    elif run is not None:
        payday_tile = Tile("Payday", "/payday", f"Done for {today:%B}", f"run {run.run_on:%-d %b}")
    elif today < payday:
        payday_tile = Tile(
            "Payday",
            "/payday",
            f"Due {payday:%-d %b}",
            f"{format_money(calc.pots_monthly_total(doc))} to top up",
        )
    else:
        late = (today - payday).days >= 1
        payday_tile = Tile(
            "Payday",
            "/payday",
            "Not yet run",
            f"{format_money(calc.pots_monthly_total(doc))} to top up",
            "urgent" if late else "warn",
        )

    balances = calc.pot_balances(doc)
    live = [p for p in doc.pots if not p.archived]
    overdrawn = [p for p in live if balances[p.id] < 0]
    savings_tile = Tile(
        "Savings",
        "/pots",
        format_money(calc.pots_total(doc)),
        f"{calc.plural(len(overdrawn), 'pot')} overdrawn"
        if overdrawn
        else f"across {calc.plural(len(live), 'pot')}",
        "urgent" if overdrawn else "",
    )

    summary = calc.summary(doc)
    money_tile = Tile(
        "In / Out",
        "/monthly",
        format_money(summary.spare),
        "spare a month" if summary.spare >= 0 else "a month more out than in",
        "urgent" if summary.spare < 0 else "",
    )

    # A due renewal (expired ones included) is named first, so the tile's
    # colour and its words are about the same thing.
    due = calc.due_renewals(doc, today)
    upcoming = calc.next_renewal(doc, today)
    if due:
        renewal = due[0]
        days = calc.days_until(renewal, today) or 0
        when = (
            f"expired {calc.plural(-days, 'day')} ago"
            if days < 0
            else "today"
            if days == 0
            else f"in {calc.plural(days, 'day')}"
        )
        # Red from two weeks out, as the attention panel has it.
        renewal_tile = Tile(
            "Renewals", "/renewals", renewal.kind, when, "urgent" if days <= 14 else "warn"
        )
    elif upcoming is not None:
        renewal, days = upcoming
        renewal_tile = Tile("Renewals", "/renewals", renewal.kind, f"in {calc.plural(days, 'day')}")
    else:
        renewal_tile = Tile(
            "Renewals", "/renewals", calc.plural(len(doc.renewals), "renewal"), "none coming up"
        )

    stale = calc.stale_wealth(doc, today) is not None
    wealth_tile = Tile(
        "Pensions",
        "/wealth",
        format_money(calc.wealth_total(doc)) if doc.wealth_accounts else "None yet",
        "figures out of date" if stale else calc.plural(len(doc.wealth_accounts), "account"),
        "warn" if stale else "",
    )

    flows = calc.monthly_flows(doc, today)
    saved = sum(f.net for f in flows)
    insights_tile = Tile(
        "Insights",
        "/insights",
        format_money(saved),
        "saved over 12 months" if saved >= 0 else "more spent than saved, 12 months",
    )

    return [
        today_tile,
        payday_tile,
        savings_tile,
        money_tile,
        renewal_tile,
        wealth_tile,
        insights_tile,
    ]


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def index(request: Request) -> Any:
    """Home: a tile for each area, each saying whether it needs opening."""
    doc, _etag = store.load()
    today = _today()
    return templates.TemplateResponse(
        request, "home.html", {"today": today, "tiles": _tiles(doc, today)}
    )


@router.get("/today", response_class=HTMLResponse, include_in_schema=False)
def today_page(request: Request) -> Any:
    """What needs attention, and what moved this week."""
    doc, _etag = store.load()
    today = _today()
    return templates.TemplateResponse(
        request,
        "today.html",
        {
            "doc": doc,
            "today": today,
            "attention": calc.attention(doc, today),
            "recent": calc.recent_movements(doc, today),
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
            amount = _amount(raw)
            if amount == 0:
                continue
            if amount < 0:
                # A payday only ever puts money in; a negative one would shrink
                # the pot while the ledger called it a contribution.
                raise InvalidInput("A payday amount can't be negative. Record a spend instead.")
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
                transfer.amount_pence = _amount(text) if text else None
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
                amount_pence=_amount(text) if text else None,
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
    balances = calc.pot_balances(doc)
    active = sorted(
        (p for p in doc.pots if not p.archived), key=lambda p: balances[p.id], reverse=True
    )
    return templates.TemplateResponse(
        request,
        "pots.html",
        {
            "doc": doc,
            "today": today,
            "active_pots": active,
            "balances": balances,
            "pots_total": calc.pots_total(doc),
            "monthly_total": calc.pots_monthly_total(doc),
            "months": calc.pots_ledger_months(doc),
            "pot_names": {p.id: p.name for p in doc.pots},
        },
    )


@router.get("/pots/{pot_id}", response_class=HTMLResponse, include_in_schema=False)
def pot_page(request: Request, pot_id: str, month: str = "") -> Any:
    """A pot and its ledger by month. `month=YYYY-MM` opens that month instead
    of the newest, which is how a delete brings you back where you were. A
    month with nothing left in it falls back to the newest.
    """
    doc, _etag = store.load()
    pot = doc.pot(pot_id)
    if pot is None:
        return _back("/pots")
    months = calc.ledger_months(doc.entries_for(pot_id))
    keys = {m.month.strftime("%Y-%m") for m in months}
    return templates.TemplateResponse(
        request,
        "pot.html",
        {
            "doc": doc,
            "pot": pot,
            "today": _today(),
            "balance": calc.pot_balance(doc, pot_id),
            "months": months,
            "open_month": month if month in keys else "",
        },
    )


@router.post("/pots/add", include_in_schema=False)
def pot_add(
    name: str = Form(...), monthly: str = Form(default=""), purpose: str = Form(default="savings")
) -> Any:
    debt, bills = _purpose(purpose)

    def change(doc: Document) -> Document:
        doc.pots.append(
            Pot(name=name.strip(), monthly_pence=_amount(monthly), debt=debt, bills=bills)
        )
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
    purpose: str = Form(default=""),
) -> Any:
    # Left out by a page loaded before the field existed: keep what is stored.
    flags = _purpose(purpose) if purpose else None

    def change(doc: Document) -> Document:
        pot = doc.pot(pot_id)
        if pot is not None:
            pot.name = name.strip()
            pot.monthly_pence = _amount(monthly)
            pot.counts_toward_payday = bool(counts)
            pot.archived = bool(archived)
            if flags is not None:
                pot.debt, pot.bills = flags
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
    value = abs(_amount(amount))
    if value == 0:
        raise InvalidInput("An amount of zero records nothing.")
    signed = -value if kind == "spend" else value
    when = _date(on, _today())

    def change(doc: Document) -> Document:
        if doc.pot(pot_id) is None:
            # Was a silent no-op: the form redirected back as if it had saved.
            raise InvalidInput("That pot no longer exists.", status.HTTP_404_NOT_FOUND)
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
def pot_entry_delete(pot_id: str, entry_id: str, month: str = Form(default="")) -> Any:
    def change(doc: Document) -> Document:
        doc.pot_entries = [e for e in doc.pot_entries if e.id != entry_id]
        # Any payday run that created it stops claiming it, so re-running that
        # month does not try to remove an entry that is already gone.
        for run in doc.payday_runs:
            if entry_id in run.entry_ids:
                run.entry_ids = [i for i in run.entry_ids if i != entry_id]
        return doc

    _apply(change)
    # Checked, since it lands in the Location header.
    reopen = f"?month={month}" if _MONTH.fullmatch(month) else ""
    return _back(f"/pots/{pot_id}{reopen}")


# --- insights -----------------------------------------------------------------


@dataclass(frozen=True)
class Share:
    """One part of the "where the money goes" bar."""

    label: str
    amount_pence: int
    percent: int  # of income, whole percent, for the bar's width and label
    css: str  # the part's colour class
    part: str  # its breakdown page under /insights/, or "" for spare


def _income_split(summary: calc.Summary) -> list[Share]:
    """Bills, savings, debt, the other payday transfers and spare as shares of
    income. Empty when there is no income, or when the plan spends more than
    comes in (the page says so instead: a bar can't show more than its whole).

    The order is the colour order the palette was validated in, which puts
    savings between bills' orange and debt's pink. With no savings the two
    touch; they are just under the palette's separation floor, so the bar's
    gaps and its legend are what tell them apart then."""
    if summary.income <= 0 or summary.spare < 0:
        return []
    parts = [
        ("Bills", summary.bills, "c-out", "bills"),
        ("Savings", summary.savings, "c-save", "savings"),
        ("Debt", summary.debt, "c-debt", "debt"),
        ("Other transfers", summary.transfers, "c-pots", "transfers"),
        ("Spare", summary.spare, "c-spare", ""),
    ]
    return [
        Share(label, amount, (amount * 100 + summary.income // 2) // summary.income, css, part)
        for label, amount, css, part in parts
        if amount > 0
    ]


@router.get("/insights", response_class=HTMLResponse, include_in_schema=False)
def insights_page(request: Request) -> Any:
    doc, _etag = store.load()
    today = _today()
    flows = calc.monthly_flows(doc, today)
    summary = calc.summary(doc)
    return templates.TemplateResponse(
        request,
        "insights.html",
        {
            "doc": doc,
            "today": today,
            "flows": flows,
            "paid_in": sum(f.paid_in for f in flows),
            "paid_out": sum(f.paid_out for f in flows),
            # Full months only, as the pots' averages are: the current month
            # is part-way through, and before payday it would drag this down.
            "average_month": calc.average(sum(f.net for f in flows[:-1]), len(flows) - 1),
            "balance_chart": charts.balance_chart(flows),
            "flow_chart": charts.flow_chart(flows),
            "health": calc.pot_health(doc, today),
            "summary": summary,
            "split": _income_split(summary),
            "W": charts.WIDTH,
            "H": charts.HEIGHT,
            "LEFT": charts.LEFT,
            "RIGHT": charts.RIGHT,
        },
    )


_PART_TITLES = {
    "bills": "Bills",
    "savings": "Savings",
    "debt": "Debt",
    "transfers": "Other transfers",
}


@router.get("/insights/{part}", response_class=HTMLResponse, include_in_schema=False)
def insights_part_page(request: Request, part: str) -> Any:
    """What makes up one part of "where the income goes", read only."""
    if part not in calc.PLAN_PARTS:
        return _back("/insights")
    doc, _etag = store.load()
    items = calc.plan_breakdown(doc, part)
    return templates.TemplateResponse(
        request,
        "breakdown.html",
        {
            "doc": doc,
            "today": _today(),
            "title": _PART_TITLES[part],
            "part": part,
            "items": items,
            "total": sum(i.amount_pence for i in items),
        },
    )


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


def _find_line(doc: Document, line_id: str) -> tuple[Line, str] | None:
    """A line and the side it is on, "income" or "outgoing"."""
    for side, lines in (("income", doc.income), ("outgoing", doc.outgoings)):
        for line in lines:
            if line.id == line_id:
                return line, side
    return None


@router.get("/monthly/{line_id}", response_class=HTMLResponse, include_in_schema=False)
def line_page(request: Request, line_id: str) -> Any:
    """One line, open for editing: the list links here, as the pots list does."""
    doc, _etag = store.load()
    found = _find_line(doc, line_id)
    if found is None:
        return _back("/monthly")
    line, side = found
    return templates.TemplateResponse(
        request, "line.html", {"doc": doc, "today": _today(), "line": line, "side": side}
    )


@router.post("/monthly/{line_id}/edit", include_in_schema=False)
def line_edit(
    line_id: str,
    name: str = Form(...),
    amount: str = Form(...),
    side: str = Form(...),
    was: str = Form(default=""),
    active: str = Form(default=""),
    debt: str = Form(default=""),
) -> Any:
    """Save a line. `was` is the side the page was rendered with: the line
    moves only when the select was changed from it, so a page opened before
    someone else moved the line doesn't move it back."""
    if side not in ("income", "outgoing"):
        raise InvalidInput("A line is either money in or money out.")

    def change(doc: Document) -> Document:
        found = _find_line(doc, line_id)
        if found is None:
            raise InvalidInput("That line no longer exists.", status.HTTP_404_NOT_FOUND)
        line, current = found
        if name.strip():
            line.name = name.strip()
        line.amount_pence = _amount(amount)
        line.active = bool(active)
        # Only money out can be a debt; a line moved to income loses the
        # flag rather than carrying one it can't show.
        moving = side != (was or current)
        target = side if moving else current
        line.debt = target == "outgoing" and bool(debt)
        if target != current:
            if current == "income":
                doc.income = [x for x in doc.income if x.id != line_id]
                doc.outgoings.append(line)
            else:
                doc.outgoings = [x for x in doc.outgoings if x.id != line_id]
                doc.income.append(line)
        return doc

    _apply(change)
    return _back("/monthly")


@router.post("/monthly/add", include_in_schema=False)
def monthly_add(
    kind: str = Form(...),
    name: str = Form(...),
    amount: str = Form(default=""),
    debt: str = Form(default=""),
) -> Any:
    def change(doc: Document) -> Document:
        line = Line(name=name.strip(), amount_pence=_amount(amount))
        if kind == "income":
            doc.income.append(line)
        else:
            line.debt = bool(debt)
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
            # Was a silent no-op that redirected back as if it had saved.
            raise InvalidInput("That account no longer exists.", status.HTTP_404_NOT_FOUND)
        current_pence = _amount(current) if current.strip() else None
        history = doc.snapshots_for(account_id)
        doc.wealth_snapshots.append(
            WealthSnapshot(
                account_id=account_id,
                as_of=when,
                current_pence=current_pence,
                yearly_projection_pence=_amount(projection) if projection.strip() else None,
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
