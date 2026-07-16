"""Exhaustive proof of cross-turn constraint memory.

Stickiness has regressed twice: bedrooms were not carried, and after that was
fixed the budget was still not carried -- the same bug one dimension over. That
is a grid problem, so it gets a grid: every conversation of up to 3 turns over
the turn archetypes, folded through merge_sticky and diffed against an
independently stated contract.

THE CONTRACT (stated, not copied from the implementation):
  The effective value of a dimension on any turn is the most recent value the
  caller STATED for it in this call. Exact and floor bedrooms are ONE dimension
  (stating either replaces the other). Saying nothing about a dimension leaves
  it as it was.
"""
import itertools

import pytest

from kb.query_parser import QueryParser
from kb.schema import QueryFilters

# (label, what the caller stated this turn)
TURNS = [
    ("apartment", {"property_type": "apartment"}),
    ("house", {"property_type": "house"}),
    ("colombo7", {"zone": 7}),
    ("colombo5", {"zone": 5}),
    ("2bed", {"bedrooms": 2}),
    ("1bed", {"bedrooms": 1}),
    ("atleast2", {"min_bedrooms": 2}),
    ("under200k", {"max_rent": 200000.0, "max_rent_exclusive": True}),
    ("upto400k", {"max_rent": 400000.0, "max_rent_exclusive": False}),
    ("over300k", {"min_rent": 300000.0}),
    ("smalltalk", {}),
]

_DIMS = ("property_type", "zone", "bedrooms", "min_bedrooms", "max_rent",
         "max_rent_exclusive", "min_rent")
_BED_DIMS = ("bedrooms", "min_bedrooms")


def _contract(sequence):
    """Most recent stated value wins. Written from the contract above."""
    eff = {d: None for d in _DIMS}
    eff["max_rent_exclusive"] = False
    for _, stated in sequence:
        for dim, value in stated.items():
            if dim == "max_rent_exclusive":
                continue
            if dim in _BED_DIMS:
                eff["bedrooms"] = eff["min_bedrooms"] = None
            eff[dim] = value
            if dim == "max_rent":
                eff["max_rent_exclusive"] = stated.get("max_rent_exclusive", False)
    return eff


def _actual(sequence):
    sticky, final = {}, None
    for _, stated in sequence:
        f = QueryFilters(**stated)
        QueryParser.merge_sticky(f, sticky)
        final = f
    return {d: getattr(final, d) for d in _DIMS}


SEQUENCES = [s for n in (1, 2, 3) for s in itertools.product(TURNS, repeat=n)]


def test_sequence_space_is_large():
    assert len(SEQUENCES) == len(TURNS) + len(TURNS) ** 2 + len(TURNS) ** 3


def test_every_conversation_up_to_three_turns():
    failures = []
    for seq in SEQUENCES:
        want, got = _contract(seq), _actual(seq)
        if want != got:
            path = " -> ".join(label for label, _ in seq)
            diff = {k: (got[k], want[k]) for k in want if got[k] != want[k]}
            failures.append(f"{path}: got/want {diff}")
    assert not failures, (
        f"{len(failures)} of {len(SEQUENCES)} conversations lost or mangled a "
        f"constraint:\n  " + "\n  ".join(failures[:20]))


def test_a_restated_dimension_always_wins():
    """Explicit guard on the override half of the contract."""
    for (l1, a), (l2, b) in itertools.product(TURNS, repeat=2):
        if not b:
            continue
        got = _actual([(l1, a), (l2, b)])
        for dim, value in b.items():
            if dim == "max_rent_exclusive":
                continue
            assert got[dim] == value, f"{l1} -> {l2}: {dim} was not overridden"


def test_bedroom_exact_and_floor_are_mutually_exclusive():
    for seq in SEQUENCES:
        got = _actual(seq)
        assert not (got["bedrooms"] is not None and got["min_bedrooms"] is not None), \
            " -> ".join(l for l, _ in seq)


@pytest.mark.parametrize("dim,turn", [
    ("property_type", "apartment"), ("zone", "colombo7"),
    ("bedrooms", "2bed"), ("max_rent", "under200k"), ("min_rent", "over300k"),
])
def test_small_talk_never_forgets_a_constraint(dim, turn):
    """The regression that shipped twice: a follow-up turn that mentions nothing
    must not silently drop what the caller already told us."""
    stated = dict(TURNS)[turn]
    got = _actual([(turn, stated), ("smalltalk", {})])
    assert got[dim] == stated[dim]
