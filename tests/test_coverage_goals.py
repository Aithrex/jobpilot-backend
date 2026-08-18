"""ADR-004 coverage_report G3/G5 are code facts, not live-Mongo claims."""
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from coverage_report import Goal, g3_vendors, g5_states  # noqa: E402


def test_g3_and_g5_are_met_from_the_registries_alone():
    g3 = Goal("G3", "ATS vendors registered", 12.0)
    g3_vendors(g3)
    assert g3.value >= 12
    assert g3.state == "MET"

    g5 = Goal("G5", "States with a working adapter", 51.0)
    g5_states(g5)
    assert g5.value == 51
    assert g5.state == "MET"
