"""Money is integer pence, and the reason is the spreadsheet's own total cell."""

from __future__ import annotations

import pytest

from finances.money import format_money, format_pounds, parse_money

# The 37 money-out amounts from the spreadsheet, as openpyxl hands them back.
OUTGOINGS = [
    80,
    933,
    200,
    161,
    84,
    78.47,
    68,
    28.36,
    15,
    40,
    5.05,
    5.99,
    22.99,
    26.99,
    24.99,
    25.04,
    127.54,
    38,
    6.9,
    47.99,
    6.9,
    6.52,
    12.25,
    38.97,
    6.62,
    8,
    1.59,
    11.45,
    27.5,
    5,
    14.8,
    120,
    259,
    30.5,
    60,
    82.12,
    12,
]


@pytest.mark.parametrize(
    ("raw", "pence"),
    [
        ("1,234.56", 123456),
        ("£450", 45000),
        ("-25", -2500),
        ("", 0),
        ("  ", 0),
        (None, 0),
        ("38.97", 3897),
        (450, 45000),
        (78.47, 7847),
        (0.1, 10),
    ],
)
def test_parse_money(raw, pence):
    assert parse_money(raw) == pence


def test_parse_money_rejects_nonsense():
    with pytest.raises(ValueError, match="not an amount"):
        parse_money("abc")


def test_outgoings_sum_exactly():
    """The whole reason for integer pence.

    Excel summed these as floats and cached 2722.5299999999997 in `E41`. Adding
    them as pence has to land on exactly 272253, or the app reproduces the bug
    it exists to avoid.
    """
    assert sum(parse_money(v) for v in OUTGOINGS) == 272253
    assert format_money(sum(parse_money(v) for v in OUTGOINGS)) == "£2,722.53"


@pytest.mark.parametrize(
    ("pence", "text"),
    [
        (595000, "£5,950"),
        (272253, "£2,722.53"),
        (-2500, "-£25"),
        (0, "£0"),
        (5, "£0.05"),
    ],
)
def test_format_money(pence, text):
    assert format_money(pence) == text


def test_format_pounds_round_trips():
    """What goes into a form field has to come back out unchanged."""
    for pence in (0, 5, 4500, 272253, -2500):
        assert parse_money(format_pounds(pence)) == pence
