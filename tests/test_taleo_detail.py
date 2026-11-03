"""Taleo's body comes from the posting's own `jobdetail.ftl` page.

The provider asked `rest/jobboard/jobDetail`, which 404s on every zone (re-probed
live 2026-11-03), so every active Taleo posting was title-only after 5-14
failed attempts. The page prints the description, URL-encoded, in its
`initialHistory` hidden field.
"""
from urllib.parse import quote

from services.ats_providers.base import Endpoint, FetchContext, RawJob
from services.ats_providers.taleo import TaleoProvider, _description_from_detail_page

DESC = "<p>Design water treatment plants.</p><ul><li>Lead a team of 12.</li></ul>" + " x" * 80
QUAL = "<p>Required Qualifications\\: Bachelor's degree, 10 years of experience.</p>" + " y" * 80


def _page(*sections, action="jobdetail.ftl"):
    fields = ["1", "2", "Drinking Water Lead"] + ["!*!" + quote(s) for s in sections] + \
             ["United States-Washington-Pasco"]
    value = "!|!".join(fields).replace('"', "&quot;")
    return (f'<form name="ftlform" id="ftlform" method="post" action="{action}">'
            f'<input type="hidden" name="initialHistory" id="initialHistory" value="{value}" /></form>')


def test_sections_are_joined_in_order_without_repeats_or_escapes():
    text = _description_from_detail_page(_page(DESC, DESC, QUAL))
    assert text.index("Design water treatment") < text.index("Required Qualifications:")
    assert text.count("Design water treatment") == 1
    assert "\\:" not in text and "Pasco" not in text


def test_a_closed_requisition_or_maintenance_page_yields_nothing():
    assert _description_from_detail_page(_page(DESC, action="unavailablerequisition.ftl")) == ""
    assert _description_from_detail_page("<title>Career Section Unavailable</title>") == ""
    assert _description_from_detail_page("<html>no history</html>") == ""


def test_fetch_detail_asks_the_section_less_page_by_job_id():
    calls = []

    def get_text(url, params=None, timeout=None, vendor="", extra_headers=None):
        calls.append((url, params))
        return 200, _page(DESC, QUAL)

    ctx = FetchContext(get_json=lambda *a, **k: (404, None), get_text=get_text)
    ep = Endpoint(vendor="taleo", tenant="hdr", host="hdr.taleo.net", extra={"portal": "101430233"})
    out = TaleoProvider().fetch_detail(ep, RawJob(source_job_id="793642", title="Lead", apply_url=""), ctx)
    assert calls == [("https://hdr.taleo.net/careersection/jobdetail.ftl", {"job": "793642", "lang": "en"})]
    assert "Bachelor's degree" in out.description
