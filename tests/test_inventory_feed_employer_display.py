"""One requisition, one card, and always the employer's own name.

JPMorgan's Oracle instance serves ONE requisition from three CX sites and
stamps each copy with a different legal entity. `_dedupe` already collapsed
them via `_req_key` (same tenant + same requisition id) — but which copy
SURVIVED was whichever arrived first, so across the six possible input orders
the kept name was "JPMorgan Chase", "CHASE FAMILY LIMITED PARTNERSHIP NO. 5"
and "NUTMEG CONTAINER CORPORATION" in two orders each.

A card showing "NUTMEG CONTAINER CORPORATION" also looks its sponsorship up
under that name and finds nothing, so the name is not cosmetic.
"""
import itertools
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared", "python"))

from services import employer_display as ED
from services import inventory_feed as IF

_CANON = "emp_31ec06b38f21"
_NAMES = [
    "JPMorgan Chase",
    "CHASE FAMILY LIMITED PARTNERSHIP NO. 5",
    "NUTMEG CONTAINER CORPORATION",
]


def _rows():
    return [
        {"job_id": chr(97 + i), "title": "Lead Software Engineer",
         "company": n, "employer_name": n, "canonical_employer_id": _CANON,
         "tenant": "jpmc", "_req_key": "req|jpmc|1", "location": "",
         "match_score": 88, "days_ago": 0}
        for i, n in enumerate(_NAMES)
    ]


def test_one_requisition_collapses_to_one_card_in_every_input_order():
    for perm in itertools.permutations(range(3)):
        rows = [dict(_rows()[i]) for i in perm]
        assert len(IF._dedupe(rows)) == 1


def test_the_surviving_card_always_shows_the_employer_not_the_legal_entity():
    for perm in itertools.permutations(range(3)):
        rows = [dict(_rows()[i]) for i in perm]
        out = IF._dedupe(rows)
        ED.apply_display_names(out)
        assert out[0]["company"] == "JPMorgan Chase", (
            f"input order {perm} kept {out[0]['company']!r}")
        assert out[0]["employer_name"] == "JPMorgan Chase"


def test_the_printed_name_is_preserved_not_discarded():
    rows = [dict(_rows()[i]) for i in (2, 0, 1)]  # NUTMEG first
    out = IF._dedupe(rows)
    ED.apply_display_names(out)
    assert out[0]["employer_name_original"] == "NUTMEG CONTAINER CORPORATION"


def test_a_name_needing_no_correction_is_left_alone():
    rows = [{"job_id": "x", "title": "SWE", "company": "Cisco",
             "employer_name": "Cisco", "canonical_employer_id": "",
             "tenant": "cisco", "_req_key": "req|cisco|9", "location": "",
             "match_score": 70, "days_ago": 0}]
    out = IF._dedupe(rows)
    ED.apply_display_names(out)
    assert out[0]["company"] == "Cisco"
    assert "employer_name_original" not in out[0]
