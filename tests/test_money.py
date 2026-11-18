"""Money is integer pence, and the reason is the spreadsheet's own total cell."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

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
        (Decimal("38.97"), 3897),
    ],
)
def test_parse_money(raw, pence):
    assert parse_money(raw) == pence


def test_parse_money_rejects_nonsense():
    with pytest.raises(ValueError, match="not an amount"):
        parse_money("abc")


@pytest.mark.parametrize("cell", [True, False])
def test_parse_money_rejects_a_boolean_cell_as_not_an_amount(cell):
    # openpyxl reads a TRUE/FALSE cell as a bool. It must fail like any other
    # nonsense, with the cell named, not as an AttributeError from deep inside.
    with pytest.raises(ValueError, match="not an amount"):
        parse_money(cell)


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


# --- properties -----------------------------------------------------------------
#
# The examples above are the cases someone thought of. These hold for every
# amount: money is the one thing in this app that must never be a penny out.

PENCE = st.integers(min_value=-(10**15), max_value=10**15)


@given(PENCE)
def test_a_form_value_parses_back_to_the_same_pence(pence: int):
    """`format_pounds` fills an <input>; what the browser posts back must be equal."""
    assert parse_money(format_pounds(pence)) == pence


@given(PENCE)
def test_a_displayed_amount_parses_back_to_the_same_pence(pence: int):
    assert parse_money(format_money(pence)) == pence


@given(st.integers(min_value=-(10**15), max_value=10**15))
def test_a_whole_number_is_pounds(pounds: int):
    assert parse_money(pounds) == pounds * 100
    assert parse_money(str(pounds)) == pounds * 100


@given(st.integers(min_value=-(10**13), max_value=10**13))
def test_a_spreadsheet_float_lands_on_the_penny_it_shows(pence: int):
    """openpyxl's floats: `38.97` must be 3897, never 3896, at any size a cell holds."""
    assert parse_money(pence / 100) == pence


@given(st.lists(PENCE, max_size=50))
def test_summing_parsed_amounts_never_drifts(amounts: list[int]):
    """The spreadsheet's £2722.5299999999997, for any column of figures."""
    assert sum(parse_money(format_pounds(p)) for p in amounts) == sum(amounts)


@given(st.text())
def test_any_text_is_an_amount_or_a_value_error(text: str):
    """A form field can hold anything. It must never be a 500."""
    try:
        result = parse_money(text)
    except ValueError:
        return
    assert isinstance(result, int)


NUMBERISH = st.one_of(
    st.from_regex(r"\A[-+]?[£]?[0-9,]{0,20}(\.[0-9]{0,6})?([eE][-+]?[0-9]{1,12})?\Z"),
    st.sampled_from(["nan", "NaN", "inf", "-Infinity", "sNaN", "1e999999999", "-0", "1_000"]),
)


@example("1e999999999")  # decimal.Overflow, found by this test
@given(NUMBERISH)
def test_anything_number_shaped_is_an_amount_or_a_value_error(text: str):
    """Exponents, NaN and infinity get through Decimal; they must not get through here."""
    try:
        result = parse_money(text)
    except ValueError:
        return
    assert isinstance(result, int)
