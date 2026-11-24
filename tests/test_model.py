"""The document survives being written and read back, whatever is in it.

Every save is `to_json`, every load and every backup restore is `from_json`, so
a value that doesn't round-trip is lost on the next save, silently.
"""

from __future__ import annotations

from datetime import date

from hypothesis import given
from hypothesis import strategies as st

from finances.model import (
    PURPOSES,
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

TEXT = st.text(max_size=30)
PENCE = st.integers(min_value=-(10**12), max_value=10**12)
DAY = st.dates(min_value=date(1990, 1, 1), max_value=date(2100, 12, 31))
# Finite only: a NaN rate is saved as null, the same "no override" an
# unparseable form value gets (see routes._parse_rate).
RATE = st.none() | st.floats(allow_nan=False, allow_infinity=False)
IDS = st.text(alphabet="0123456789abcdef", min_size=8, max_size=8)

LINES = st.builds(
    Line,
    id=IDS,
    name=TEXT,
    amount_pence=PENCE,
    active=st.booleans(),
    note=TEXT,
    category=st.none() | st.sampled_from(PURPOSES),
    debt=st.booleans(),
)

documents = st.builds(
    Document,
    schema_version=st.integers(min_value=1, max_value=9),
    income=st.lists(LINES, max_size=5),
    outgoings=st.lists(LINES, max_size=5),
    transfers=st.lists(
        st.builds(
            Transfer,
            id=IDS,
            label=TEXT,
            amount_pence=st.none() | PENCE,
            note=TEXT,
            tracks_pots=st.booleans(),
        ),
        max_size=4,
    ),
    pots=st.lists(
        st.builds(
            Pot,
            id=IDS,
            name=TEXT,
            monthly_pence=PENCE,
            counts_toward_payday=st.booleans(),
            archived=st.booleans(),
            category=st.none() | st.sampled_from(PURPOSES),
            debt=st.booleans(),
            bills=st.booleans(),
        ),
        max_size=4,
    ),
    pot_entries=st.lists(
        st.builds(
            PotEntry,
            id=IDS,
            pot_id=IDS,
            on=DAY,
            amount_pence=PENCE,
            kind=st.sampled_from(["payday", "deposit", "spend", "opening", "adjustment"]),
            note=TEXT,
        ),
        max_size=10,
    ),
    payday_runs=st.lists(
        st.builds(
            PaydayRun,
            id=IDS,
            month=DAY.map(lambda d: f"{d:%Y-%m}"),
            run_on=DAY,
            entry_ids=st.lists(IDS, max_size=3),
        ),
        max_size=3,
    ),
    renewals=st.lists(
        st.builds(
            Renewal,
            id=IDS,
            kind=TEXT,
            company=TEXT,
            expires_on=st.none() | DAY,
            rolling=st.booleans(),
            comment=TEXT,
            notice_days=st.none() | st.integers(min_value=0, max_value=365),
        ),
        max_size=4,
    ),
    wealth_accounts=st.lists(
        st.builds(
            WealthAccount,
            id=IDS,
            company=TEXT,
            notes=TEXT,
            still_contributing=st.booleans(),
            editable=st.booleans(),
            target_retirement_year=st.none() | st.integers(min_value=2000, max_value=2100),
            assumed_growth_rate=RATE,
        ),
        max_size=3,
    ),
    wealth_snapshots=st.lists(
        st.builds(
            WealthSnapshot,
            id=IDS,
            account_id=IDS,
            as_of=DAY,
            current_pence=st.none() | PENCE,
            yearly_projection_pence=st.none() | PENCE,
            year_growth=RATE,
        ),
        max_size=5,
    ),
)


@given(documents)
def test_a_saved_document_loads_back_identical(doc: Document):
    assert Document.from_json(doc.to_json()) == doc


@given(documents)
def test_saving_is_stable(doc: Document):
    """Load-then-save writes the same bytes, so blob versions only differ on real edits."""
    once = doc.to_json()
    assert Document.from_json(once).to_json() == once


def test_a_document_from_before_debt_reads_as_no_debt():
    """Lines and pots stored before `debt` existed must load unchanged."""
    raw = (
        '{"outgoings": [{"id": "abcd1234", "name": "Gym", "amount_pence": 2500}],'
        ' "pots": [{"id": "abcd5678", "name": "Car"}]}'
    )
    doc = Document.from_json(raw)
    assert doc.outgoings[0].debt is False
    assert doc.pots[0].debt is False
    assert doc.pots[0].bills is False
