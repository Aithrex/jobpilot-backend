import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "repair_vetoed_software_titles",
    Path(__file__).resolve().parents[1] / "scripts" / "repair_vetoed_software_titles.py")
repair = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(repair)


def _doc(title, scope="non_tech", seniority=""):
    return {"title_raw": title, "tech": {"tech_scope": scope, "seniority": "entry"},
            "seniority": seniority}


def test_a_vetoed_software_title_is_refiled_and_keeps_its_level():
    verdict = repair.new_tech(_doc("Software Engineer, Test Frameworks & Tooling"))
    assert verdict["tech_scope"] == "core" and verdict["seniority"] == "entry"


def test_everything_else_is_left_alone():
    assert repair.new_tech(_doc("Tooling Engineer")) is None
    assert repair.new_tech(_doc("Mechanical Engineer")) is None
    assert repair.new_tech(_doc("Software Engineer, Tooling", scope="core")) is None


def test_only_an_unlevelled_class_year_intake_owes_a_level():
    assert repair.owes_a_level(_doc("Software Engineer Grad 2027"))
    assert not repair.owes_a_level(_doc("Software Engineer Grad 2027", seniority="entry"))
    assert not repair.owes_a_level(_doc("Graduate School Advisor"))
