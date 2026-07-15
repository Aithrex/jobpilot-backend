"""End-to-end tests for the real Playwright BrowserExecutor.

These drive an ACTUAL headless Chromium against local Greenhouse-style form
fixtures (file://) — proving the fill → select → attach → submit → confirm →
screenshot loop, plus CAPTCHA-wall and validation-failure detection. No network,
no employer is contacted (the fixtures are local HTML).

Skipped automatically where Chromium can't launch (e.g. CI without browsers), so
this never breaks the offline suite; it runs wherever Playwright is installed.
"""
import pathlib

import pytest

pytest.importorskip("playwright.sync_api")

from services.browser_executor import BrowserExecutor
from services.executor_adapter import (
    SUBMIT_SUCCESS, SUBMIT_FAILED, SUBMIT_WAITING_VERIFY,
    EXECUTOR_UNAVAILABLE, WALL_CAPTCHA,
)

_FIX = pathlib.Path(__file__).parent / "fixtures"
_PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"


def _browser_ok() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch(headless=True).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _browser_ok(), reason="Chromium unavailable")


def _uri(name: str) -> str:
    return (_FIX / name).as_uri()


def _executor(shots: list):
    return BrowserExecutor(
        resume_resolver=lambda app, ref, ue: ("resume.pdf", _PDF),
        screenshot_sink=lambda app, png: (shots.append(len(png)), "s3://audit/proof.png")[1],
        user_email="ada@aspirely.me",
        headless=True,
    )


_APP = {"application_id": "app1", "apply_url": "https://job-boards.greenhouse.io/acme/jobs/1",
        "ats_family": "greenhouse"}
_ANSWERS = {"first_name": "Ada", "last_name": "Lovelace",
            "email": "ada@example.com", "phone": "5551234", "question_100": "Yes"}


def test_greenhouse_submit_success_full_loop():
    shots = []
    ex = _executor(shots)
    res = ex._drive_greenhouse(_uri("gh_form_ok.html"), _ANSWERS, ("resume.pdf", _PDF), _APP)

    assert res["status"] == SUBMIT_SUCCESS
    # The confirmation echoes what actually landed in the form → proves the
    # text fill, the react-select choice, AND the file attach all worked.
    assert "Ada" in res["confirmation"]
    assert "resume.pdf" in res["confirmation"]
    assert "Yes" in res["confirmation"]
    # Screenshot proof captured and handed back as the audit ref.
    assert res["screenshot_ref"] == "s3://audit/proof.png"
    assert shots and shots[0] > 0
    assert res["wall"] is None and res["error"] is None


def test_greenhouse_captcha_wall_is_detected_not_submitted():
    ex = _executor([])
    res = ex._drive_greenhouse(_uri("gh_form_captcha.html"), _ANSWERS, ("resume.pdf", _PDF), _APP)
    assert res["status"] == SUBMIT_WAITING_VERIFY
    assert res["wall"]["kind"] == WALL_CAPTCHA
    assert res["confirmation"] is None


def test_greenhouse_validation_failure_when_required_missing():
    ex = _executor([])
    # Omit email → the form blocks submit; executor must report failure, not
    # a false success (this is what makes the "retry if it didn't submit" loop
    # trustworthy).
    answers = {k: v for k, v in _ANSWERS.items() if k != "email"}
    res = ex._drive_greenhouse(_uri("gh_form_ok.html"), answers, ("resume.pdf", _PDF), _APP)
    assert res["status"] == SUBMIT_FAILED
    assert "validation" in (res["error"] or "") or "no_confirmation" in (res["error"] or "")


def test_lever_submit_success_full_loop():
    shots = []
    ex = _executor(shots)
    answers = {"name": "Ada Lovelace", "email": "ada@example.com",
               "phone": "5551234567", "eeo[gender]": "Female", "pronouns": "they/them"}
    res = ex._drive_lever(_uri("lever_form_ok.html"), answers, ("resume.pdf", _PDF), _APP)
    assert res["status"] == SUBMIT_SUCCESS
    # confirmation echoes the native-select choice + name + file → fill worked
    assert "Ada Lovelace" in res["confirmation"]
    assert "resume.pdf" in res["confirmation"]
    assert "Female" in res["confirmation"]
    assert res["screenshot_ref"] == "s3://audit/proof.png"


def test_lever_recon_extracts_questions():
    ex = _executor([])
    res = ex._recon_browser(_uri("lever_form_ok.html"), "lever", {"resume", "cover_letter"}, ("eeo",))
    assert "status" not in res  # recon succeeded
    q = {x["id"]: x for x in res["questions"]}
    assert "name" in q and "email" in q and "phone" in q
    assert "resume" not in q  # file field excluded (executor-supplied)
    assert q["eeo[gender]"]["type"] == "select"
    assert q["eeo[gender]"]["is_legal_attestation"] is True  # EEO → legal
    for x in res["questions"]:
        assert set(x) == {"id", "text", "type", "required", "is_legal_attestation"}


def test_submit_family_gate_unsupported_is_unavailable():
    ex = _executor([])
    # Workday has no driver yet → clean unavailable (no browser launched).
    app = {"application_id": "x",
           "apply_url": "https://acme.wd1.myworkdayjobs.com/en-US/careers/job/1",
           "ats_family": "workday"}
    res = ex.submit(app, _ANSWERS, {"tailoring_record_id": "t1"})
    assert res["status"] == EXECUTOR_UNAVAILABLE


def test_submit_routes_greenhouse_to_driver(monkeypatch):
    ex = _executor([])
    captured = {}

    def _fake_drive(url, answers, resume, app):
        captured["url"] = url
        return {"status": SUBMIT_SUCCESS, "confirmation": "ok",
                "screenshot_ref": None, "wall": None, "error": None}

    monkeypatch.setattr(ex, "_drive_greenhouse", _fake_drive)
    res = ex.submit(_APP, _ANSWERS, {"tailoring_record_id": "t1", "user_email": "ada@aspirely.me"})
    assert res["status"] == SUBMIT_SUCCESS
    # apply_url normalized to the canonical job-boards form URL from the ids.
    assert captured["url"] == "https://job-boards.greenhouse.io/acme/jobs/1"


def test_is_available_reflects_enabled_flag():
    assert BrowserExecutor(enabled=True).is_available() is True
    assert BrowserExecutor(enabled=False).is_available() is False
