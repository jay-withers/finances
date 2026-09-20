"""Money, as integer pence.

**Nothing here uses float.** A household budget is a long chain of additions
that has to reconcile against a bank statement to the penny, and binary floats
do not represent 0.1 exactly: summing the 38 outgoing lines as floats drifts in
the last place, which is exactly the "£2722.5299999999997" the spreadsheet shows
in its own total cell. Pence are integers, so the arithmetic is exact and the
formatting happens once, at the edge.

`Decimal` would also be exact, but it serialises to JSON as a string and invites
mixed-type arithmetic. An int cannot be accidentally added to a float and stay
plausible.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

__all__ = ["format_money", "format_pounds", "parse_money"]


def parse_money(value: str | int | float | Decimal | None) -> int:
    """Pence from anything a form field or a spreadsheet cell might hold.

    Accepts `"1,234.56"`, `"£450"`, `"-25"`, `450`, and `Decimal("38.97")`.
    Blank, None and whitespace are 0 — a blank cell in the spreadsheet's
    "Volleyball" row means no payment, not a malformed one.

    `float` is accepted because openpyxl hands back floats for numeric cells,
    and quantising one at the boundary is the whole point: it is converted via
    `Decimal(str(value))`, not `Decimal(value)`, so 38.97 becomes 3897 rather
    than 3896.999...
    """
    if value is None:
        return 0
    if isinstance(value, int) and not isinstance(value, bool):
        return value * 100

    if isinstance(value, float):
        # str() first: Decimal(38.97) is the binary approximation, and rounding
        # that lands a penny out often enough to matter over 38 lines.
        text = str(value)
    elif isinstance(value, Decimal):
        text = str(value)
    else:
        text = value.strip().replace(",", "").replace("£", "").replace(" ", "")
        if text in {"", "-", "."}:
            return 0

    try:
        return int((Decimal(text) * 100).quantize(Decimal("1")))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"not an amount: {value!r}") from exc


def format_money(pence: int) -> str:
    """`"£1,234.56"`, or `"£450"` when the amount is whole pounds.

    Trailing `.00` is dropped because most figures here are whole pounds, and a
    column of "£450.00 £100.00 £130.00" is harder to scan than the same column
    without. The pence are shown whenever they exist, which is what matters.
    """
    sign = "-" if pence < 0 else ""
    pounds, remainder = divmod(abs(pence), 100)
    if remainder:
        return f"{sign}£{pounds:,}.{remainder:02d}"
    return f"{sign}£{pounds:,}"


def format_pounds(pence: int) -> str:
    """`"1234.56"` — the bare number for a form's `value` attribute.

    Always two decimal places and no separators, because this is parsed back by
    a browser's `<input type="number">` rather than read by a person.
    """
    sign = "-" if pence < 0 else ""
    pounds, remainder = divmod(abs(pence), 100)
    return f"{sign}{pounds}.{remainder:02d}"
