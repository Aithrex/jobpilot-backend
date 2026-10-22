"""URL trust at the security boundary + contextual fraud indicators (trust-1.0.0).

Every gap in docs/audit/jobright-2026-10-21/CLAUDE-PROMPT.txt ("REPRODUCED
CODE GAPS" 2 and 3) was reproduced on the base tree before these tests were
written, with these exact results from the old `assess_job`:

  * https://greenhouse.io.attacker.example/apply          -> trusted / 92
  * https://attacker.example/apply?return=greenhouse.io   -> trusted / 92
  * source="Jobright", 5-char description                 -> trusted / 86
  * "We will never ask you to pay a registration fee or
     share your social security number."                  -> high_risk / 20
  * Meta "Software Engineer, WhatsApp"                    -> high_risk / 20

Pure functions only: no network, no DB, no LLM.
"""
from __future__ import annotations

import pytest

from services import job_legitimacy_service as jl
from services import ssrf_guard

LONG = ("We are hiring an engineer to build distributed systems that serve "
        "millions of requests a day. You will design APIs, review code, and "
        "work with product and infrastructure teams. ") * 4


def _doc(**kw):
    base = {"title_raw": "Software Engineer", "employer_name": "Acme",
            "description_text": LONG}
    base.update(kw)
    return base


def _codes(trust):
    return [w["code"] for w in trust["warnings"]]


# ---------------------------------------------------------------------------
# URL parsing helpers (ssrf_guard is the home of the suffix-boundary matcher)
# ---------------------------------------------------------------------------
class TestHostParsing:
    @pytest.mark.parametrize("url,host", [
        ("https://boards.greenhouse.io/acme/jobs/1", "boards.greenhouse.io"),
        ("https://BOARDS.Greenhouse.IO./acme", "boards.greenhouse.io"),
        ("https://greenhouse.io@attacker.example/x", "attacker.example"),
        ("https://bоards.greenhouse.io/x", "xn--bards-jye.greenhouse.io"),  # Cyrillic о
    ])
    def test_url_host_parses_and_normalizes(self, url, host):
        assert ssrf_guard.url_host(url) == host

    @pytest.mark.parametrize("url", [
        "javascript:alert(1)//boards.greenhouse.io",
        "ftp://boards.greenhouse.io/x",
        "data:text/html,boards.greenhouse.io",
        "//boards.greenhouse.io/x",
        "boards.greenhouse.io/acme",
        # parser differential: urllib says greenhouse, a browser says attacker
        "https://attacker.example\\@boards.greenhouse.io/x",
        "https://boards.greenhouse.io%2eattacker.example/",
        "https://a b.greenhouse.io/",
        "",
        None,
    ])
    def test_url_host_refuses_non_web_and_ambiguous(self, url):
        assert ssrf_guard.url_host(url) == ""

    @pytest.mark.parametrize("host,domain,ok", [
        ("greenhouse.io", "greenhouse.io", True),
        ("boards.greenhouse.io", "greenhouse.io", True),
        ("job-boards.eu.greenhouse.io", "greenhouse.io", True),
        ("acme.wd5.myworkdayjobs.com", "myworkdayjobs.com", True),
        ("greenhouse.io.attacker.example", "greenhouse.io", False),
        ("evilgreenhouse.io", "greenhouse.io", False),
        ("greenhouse-io.com", "greenhouse.io", False),
        ("", "greenhouse.io", False),
        ("greenhouse.io", "", False),
    ])
    def test_boundary_aware_suffix_match(self, host, domain, ok):
        assert ssrf_guard.host_matches_domain(host, domain) is ok

    def test_envelope_gate_rejects_backslash_userinfo_differential(self):
        """`urlparse` reads the host after the LAST '@'; Chromium treats '\\'
        as a path separator and navigates to attacker.example. The auto-apply
        envelope used `parsed.hostname`, so this passed the allow-list."""
        ok, _ = ssrf_guard.apply_url_allowed(
            "https://attacker.example\\@boards.greenhouse.io/acme/jobs/1")
        assert ok is False

    def test_envelope_gate_rejects_userinfo(self):
        ok, reason = ssrf_guard.apply_url_allowed(
            "https://user@boards.greenhouse.io/acme/jobs/1")
        assert ok is False and reason == "userinfo_not_allowed"

    def test_envelope_gate_still_accepts_real_ats(self):
        assert ssrf_guard.apply_url_allowed(
            "https://job-boards.eu.greenhouse.io/cognite/jobs/1")[0] is True
        assert ssrf_guard.apply_url_allowed(
            "https://acme.wd5.myworkdayjobs.com/en-US/External/job/X_R1")[0] is True


# ---------------------------------------------------------------------------
# ATS vendor detection — a host proves the VENDOR, never board ownership
# ---------------------------------------------------------------------------
class TestAtsVendorHost:
    @pytest.mark.parametrize("url,vendor", [
        ("https://boards.greenhouse.io/acme/jobs/123", "greenhouse"),
        ("https://job-boards.eu.greenhouse.io/cognite/jobs/123", "greenhouse"),
        ("https://acme.wd5.myworkdayjobs.com/External/job/X_R1", "workday"),
        ("https://jobs.lever.co/acme/abc", "lever"),
        ("https://jobs.ashbyhq.com/acme/abc", "ashby"),
        ("https://careers-acme.icims.com/jobs/1/job", "icims"),
        ("https://eeho.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/X/job/1",
         "oracle"),
    ])
    def test_real_vendor_hosts(self, url, vendor):
        assert jl.ats_vendor_of_url(url) == vendor

    @pytest.mark.parametrize("url", [
        "https://greenhouse.io.attacker.example/apply",
        "https://attacker.example/apply?return=greenhouse.io",
        "https://attacker.example/greenhouse.io/jobs/1",
        "https://attacker.example/#boards.greenhouse.io",
        "https://greenhouse.io@attacker.example/apply",
        "https://attacker.example\\@boards.greenhouse.io/apply",
        "https://greenhouse-io.com/apply",
        "https://xn--greenhuse-q6g.io/apply",
        "http://evilgreenhouse.io/apply",
        "javascript:alert('boards.greenhouse.io')",
        # Oracle Cloud object storage is anyone's bucket, not an ATS.
        "https://objectstorage.us-ashburn-1.oraclecloud.com/n/x/b/y/o/apply.html",
    ])
    def test_lookalikes_and_tricks_are_not_vendors(self, url):
        assert jl.ats_vendor_of_url(url) == ""

    def test_known_ats_host_is_not_ownership(self):
        """A genuine Greenhouse host is not evidence that Acme owns the board;
        with no canonical employer it earns no ownership and no fraud verdict."""
        t = jl.assess_posting_trust(_doc(
            canonical_url="https://boards.greenhouse.io/somebodyelse/jobs/1"))
        assert t["state"] == "no_warnings_found"
        assert "verified" not in repr(t).lower()


# ---------------------------------------------------------------------------
# The reproduced gaps through the legacy surface (`assess_job`)
# ---------------------------------------------------------------------------
class TestLegacyAssessJobGaps:
    def _job(self, **kw):
        base = {"title": "Software Engineer", "company": "Acme",
                "description": LONG, "salary": ""}
        base.update(kw)
        return base

    def test_lookalike_host_earns_no_trust_bonus(self):
        real = jl.assess_job(self._job(
            apply_link="https://boards.greenhouse.io/acme/jobs/1"))
        fake = jl.assess_job(self._job(
            apply_link="https://greenhouse.io.attacker.example/apply"))
        assert fake["trust_score"] < real["trust_score"]
        assert fake["trust_band"] == "high_risk"
        assert "impersonated_host" in fake["trust_flags"]

    def test_query_string_vendor_text_earns_no_trust_bonus(self):
        real = jl.assess_job(self._job(
            apply_link="https://boards.greenhouse.io/acme/jobs/1"))
        tricked = jl.assess_job(self._job(
            apply_link="https://attacker.example/apply?return=greenhouse.io"))
        assert tricked["trust_score"] < real["trust_score"]

    @pytest.mark.parametrize("label", ["Jobright", "jobright", "JSearch", "LinkedIn",
                                       "greenhouse", "Company Careers"])
    def test_source_label_earns_no_trust_bonus(self, label):
        bare = jl.assess_job(self._job(description="Short."))
        labelled = jl.assess_job(self._job(description="Short.", source=label))
        assert labelled["trust_score"] == bare["trust_score"]
        assert labelled["trust_flags"] == bare["trust_flags"]

    def test_anti_scam_disclaimer_is_not_high_risk(self):
        res = jl.assess_job(self._job(description=LONG + (
            " We will never ask you to pay a registration fee or share your "
            "social security number.")))
        assert res["trust_band"] != "high_risk"
        assert "asks_for_payment" not in res["trust_flags"]
        assert "financial_harvest" not in res["trust_flags"]

    def test_meta_whatsapp_product_role_is_not_high_risk(self):
        res = jl.assess_job(self._job(title="Software Engineer, WhatsApp",
                                      company="Meta",
                                      apply_link="https://www.metacareers.com/jobs/1"))
        assert res["trust_band"] != "high_risk"
        assert "offplatform_contact" not in res["trust_flags"]

    def test_real_payment_demand_is_still_high_risk(self):
        res = jl.assess_job(self._job(description=LONG + (
            " Before your interview you must pay a $95 registration fee via Zelle.")))
        assert res["trust_band"] == "high_risk"
        assert "asks_for_payment" in res["trust_flags"]

    def test_output_keys_unchanged(self):
        res = jl.assess_job(self._job())
        assert set(res) == {"trust_score", "trust_band", "trust_flags", "trust_reasons"}
        assert res["trust_band"] in {"trusted", "caution", "high_risk"}

    def test_reasons_never_claim_verification(self):
        for job in (self._job(), self._job(apply_link="https://jobs.lever.co/acme/1"),
                    self._job(canonical_employer_id="emp_0123456789ab")):
            res = jl.assess_job(job)
            assert not any("verif" in r.lower() for r in res["trust_reasons"])


# ---------------------------------------------------------------------------
# assess_posting_trust — the §C4 block
# ---------------------------------------------------------------------------
class TestAssessPostingTrustShape:
    def test_shape_and_version(self):
        t = jl.assess_posting_trust(_doc())
        assert set(t) == {"state", "warnings", "version"}
        assert t["version"] == "trust-1.0.0"
        assert t["state"] == "no_warnings_found"
        assert t["warnings"] == []

    def test_warning_items_are_explainable(self):
        t = jl.assess_posting_trust(_doc(description_text=LONG + (
            " To be considered, send $150 for the training kit via Cash App.")))
        assert t["state"] == "suspected_fraud"
        w = t["warnings"][0]
        assert set(w) == {"code", "evidence", "source"}
        assert w["source"] == "rule"
        assert "Cash App" in w["evidence"]
        assert len(w["evidence"]) <= 240

    def test_does_not_mutate_input(self):
        d = _doc(description_text="Please pay a $50 application fee.")
        before = dict(d)
        jl.assess_posting_trust(d)
        assert d == before

    @pytest.mark.parametrize("bad", [None, {}, "x", 5, {"description_text": None}])
    def test_never_raises_on_junk(self, bad):
        t = jl.assess_posting_trust(bad)
        assert t["state"] in {"no_warnings_found", "warnings", "ownership_conflict",
                              "suspected_fraud"}

    def test_reads_legacy_description_field(self):
        t = jl.assess_posting_trust({"title": "Data Entry",
                                     "description": "Kindly pay the $80 processing fee "
                                                    "to receive your offer letter."})
        assert t["state"] == "suspected_fraud"


class TestOwnershipConflict:
    def test_stored_conflict_marker(self):
        t = jl.assess_posting_trust(_doc(
            ats="greenhouse", tenant="shared", ownership_conflict=True))
        assert t["state"] == "ownership_conflict"
        assert t["warnings"][0]["code"] == "ownership_conflict"
        assert t["warnings"][0]["source"] == "ownership"

    def test_quarantine_reason_marker_from_canonical_employer(self):
        from services.canonical_employer import Q_CONFLICTING_OWNERS
        t = jl.assess_posting_trust(_doc(
            ats="greenhouse", tenant="shared",
            canonical_employer_quarantine=Q_CONFLICTING_OWNERS))
        assert t["state"] == "ownership_conflict"

    def test_marker_from_in_memory_board_map(self):
        from services.canonical_employer import BoardOwnership
        own = BoardOwnership({("greenhouse", "shared"): {"emp_a": False, "emp_b": False},
                              ("greenhouse", "solo"): {"emp_c": True},
                              ("lever", "settled"): {"emp_d": True, "emp_e": False}})
        assert jl.board_ownership_conflict({"ats": "greenhouse", "tenant": "Shared"}, own)
        assert not jl.board_ownership_conflict({"ats": "greenhouse", "tenant": "solo"}, own)
        # settled by domain-chain precedence: attributed, so not shown as contested
        assert not jl.board_ownership_conflict({"ats": "lever", "tenant": "settled"}, own)
        assert not jl.board_ownership_conflict({"ats": "greenhouse", "tenant": "x"}, None)

    def test_fraud_outranks_conflict(self):
        t = jl.assess_posting_trust(_doc(
            ownership_conflict=True,
            description_text="Send a photo of your SSN card to apply."))
        assert t["state"] == "suspected_fraud"
        assert {"ownership_conflict", "credential_request"} <= set(_codes(t))


class TestImpersonation:
    @pytest.mark.parametrize("url", [
        "https://greenhouse.io.attacker.example/apply",
        "https://boards.greenhouse.io.jobs-portal.example/acme",
        "https://greenhouse.io@attacker.example/apply",
        "https://xn--greenhuse-q6g.io/apply",
    ])
    def test_vendor_impersonation_is_suspected_fraud(self, url):
        t = jl.assess_posting_trust(_doc(canonical_url=url))
        assert t["state"] == "suspected_fraud", t
        assert set(_codes(t)) & {"impersonated_host", "deceptive_url"}

    def test_employer_domain_impersonation(self):
        t = jl.assess_posting_trust(_doc(
            employer_name="Google", employer_domain="google.com",
            canonical_url="https://google.com.careers-apply.example/job/1"))
        assert t["state"] == "suspected_fraud"
        assert "impersonated_host" in _codes(t)

    def test_employer_own_domain_is_fine(self):
        t = jl.assess_posting_trust(_doc(
            employer_name="Google", employer_domain="google.com",
            canonical_url="https://www.google.com/about/careers/applications/jobs/1"))
        assert t["state"] == "no_warnings_found"

    def test_query_string_trick_is_not_fraud_just_not_trusted(self):
        t = jl.assess_posting_trust(_doc(
            canonical_url="https://attacker.example/apply?return=greenhouse.io"))
        assert t["state"] == "no_warnings_found"

    def test_hyphen_lookalike_is_a_warning(self):
        t = jl.assess_posting_trust(_doc(canonical_url="https://greenhouse-io.com/apply"))
        assert t["state"] == "warnings"
        assert "lookalike_host" in _codes(t)


# ---------------------------------------------------------------------------
# Contextual text rules
# ---------------------------------------------------------------------------
DISCLAIMERS = [
    "We will never ask you to pay a registration fee or share your social security number.",
    "Acme never charges candidates a fee and will never ask for your bank account details.",
    "Please note: we do not require any payment, equipment purchase or training fee "
    "from applicants.",
    "Recruitment fraud warning: Acme will not request money, gift cards or cryptocurrency "
    "from job seekers.",
    "If anyone asks you to pay for training or equipment, it is a scam - report it to "
    "security@acme.com.",
    "Legitimate recruiters will never contact you on Telegram or WhatsApp.",
    "There is no application fee.",
    "We don't ask for your SSN until after you accept a written offer.",
]


@pytest.mark.parametrize("sentence", DISCLAIMERS)
def test_anti_scam_disclaimers_do_not_warn(sentence):
    t = jl.assess_posting_trust(_doc(description_text=LONG + " " + sentence))
    assert t["state"] == "no_warnings_found", t


@pytest.mark.parametrize("sentence", [
    'Scammers often write "pay a $200 registration fee to start" - ignore them.',
    "A common phishing line is “send us your bank login to set up payroll”.",
])
def test_quoted_warnings_do_not_warn(sentence):
    t = jl.assess_posting_trust(_doc(description_text=LONG + " " + sentence))
    assert t["state"] == "no_warnings_found", t


@pytest.mark.parametrize("title,sentence", [
    ("Software Engineer, WhatsApp", "You will build features for WhatsApp Business."),
    ("Software Engineer, WhatsApp", LONG),
    ("Android Engineer, Signal", "Help millions communicate privately with Signal."),
    ("Backend Engineer", "Experience with the Telegram Bot API is a plus."),
    ("Fraud Analyst", "Investigate gift card fraud, wire transfer scams and account "
                      "takeovers; identify requests for bank credentials in phishing."),
    ("Payments Engineer", "You will build services that move funds between bank accounts "
                          "and process payments for merchants."),
    ("Trust & Safety Specialist", "Review reports of users asked to pay registration fees "
                                  "and remove scam listings."),
])
def test_products_and_duties_do_not_warn(title, sentence):
    t = jl.assess_posting_trust(_doc(title_raw=title, description_text=sentence))
    assert t["state"] == "no_warnings_found", t


@pytest.mark.parametrize("sentence,code", [
    ("You must pay a $99 training fee before your first day.", "payment_request"),
    ("Please purchase your own laptop from our vendor; you will be reimbursed later.",
     "payment_request"),
    ("Send the gift card codes to the HR manager to activate your account.",
     "payment_request"),
    ("A refundable security deposit of $250 is required to receive your equipment.",
     "payment_request"),
    ("Kindly provide your bank account number and routing number to be considered.",
     "credential_request"),
    ("Share your online banking username and password so we can set up payroll.",
     "credential_request"),
    ("Applicants must submit their Social Security number with the application.",
     "credential_request"),
])
def test_genuine_demands_are_suspected_fraud(sentence, code):
    t = jl.assess_posting_trust(_doc(description_text=LONG + " " + sentence))
    assert t["state"] == "suspected_fraud", t
    assert code in _codes(t)
    ev = next(w["evidence"] for w in t["warnings"] if w["code"] == code)
    assert ev.strip() and ev in (LONG + " " + sentence)


@pytest.mark.parametrize("sentence", [
    "Contact the recruiter on WhatsApp at +1 555 0100 to proceed with your application.",
    "Message our HR coordinator on Telegram (@acme_hr) to schedule the interview.",
])
def test_offplatform_instruction_is_a_warning_not_fraud(sentence):
    t = jl.assess_posting_trust(_doc(description_text=LONG + " " + sentence))
    assert t["state"] == "warnings", t
    assert "offplatform_contact" in _codes(t)


@pytest.mark.parametrize("sentence", [
    "This is a W2-only contract position; no C2C or third parties.",
    "Contract-to-hire, 6 months, W2 only.",
    "Send your resume to hiring.acme@gmail.com.",
    "Posted 120 days ago.",
    "Over 200 applicants.",
    "We are a small, five-person startup.",
])
def test_weak_signals_alone_do_not_warn(sentence):
    t = jl.assess_posting_trust(_doc(description_text=sentence))
    assert t["state"] == "no_warnings_found", t


def test_sparse_unknown_company_old_posting_does_not_warn():
    t = jl.assess_posting_trust({"title_raw": "Engineer", "employer_name": "",
                                 "posted_at": "2023-01-01", "description_text": ""})
    assert t["state"] == "no_warnings_found"


def test_blocked_verification_is_not_a_warning():
    t = jl.assess_posting_trust(_doc(last_check_status=403, availability={"state": "unverifiable"}))
    assert t["state"] == "no_warnings_found"


def test_source_label_has_no_effect_on_trust():
    a = jl.assess_posting_trust(_doc(source="jobright"))
    b = jl.assess_posting_trust(_doc(source="greenhouse"))
    assert a == b


def test_post_offer_ssn_is_not_a_warning():
    t = jl.assess_posting_trust(_doc(description_text=(
        "After you accept the offer you will complete an I-9 and provide your Social "
        "Security number for payroll.")))
    assert t["state"] == "no_warnings_found", t


def test_big_text_is_bounded_and_fast():
    import time
    blob = (LONG + " ") * 400
    t0 = time.perf_counter()
    jl.assess_posting_trust(_doc(description_text=blob))
    assert time.perf_counter() - t0 < 0.5


# ---------------------------------------------------------------------------
# Liveness probe: redirects followed hop-by-hop through the SSRF guard
# ---------------------------------------------------------------------------
class _Resp:
    def __init__(self, status, location=""):
        self.status_code = status
        self.headers = {"Location": location} if location else {}

    def close(self):
        pass


class TestProbeRedirects:
    def _policy(self):
        from services.job_ingest_worker import HostPolicy
        return HostPolicy()

    def test_redirect_to_metadata_is_refused(self, monkeypatch):
        import requests
        from services import job_ingest_worker as w
        from services import jd_fetcher
        calls = []

        def fake(method, url, **kw):
            calls.append(url)
            assert kw.get("allow_redirects") is False
            return _Resp(302, "http://169.254.169.254/latest/meta-data/")

        monkeypatch.setattr(requests, "request", fake)
        monkeypatch.setattr(jd_fetcher.socket, "getaddrinfo",
                            lambda h, p, **k: [(2, 1, 6, "", ("104.18.1.1", p))])
        import time
        status = w.http_probe_status(
            "greenhouse", "https://boards.greenhouse.io/acme/jobs/1", self._policy(),
            deadline=time.monotonic() + 20)
        assert status is None
        assert calls == ["https://boards.greenhouse.io/acme/jobs/1"]

    def test_public_redirect_is_followed_and_final_status_returned(self, monkeypatch):
        import requests
        from services import job_ingest_worker as w
        from services import jd_fetcher
        seq = [_Resp(301, "https://job-boards.greenhouse.io/acme/jobs/1"), _Resp(404)]
        calls = []

        def fake(method, url, **kw):
            calls.append((method, url))
            return seq.pop(0)

        monkeypatch.setattr(requests, "request", fake)
        monkeypatch.setattr(jd_fetcher.socket, "getaddrinfo",
                            lambda h, p, **k: [(2, 1, 6, "", ("104.18.1.1", p))])
        import time
        status = w.http_probe_status(
            "greenhouse", "https://boards.greenhouse.io/acme/jobs/1", self._policy(),
            deadline=time.monotonic() + 20)
        assert status == 404
        assert [c[1] for c in calls] == ["https://boards.greenhouse.io/acme/jobs/1",
                                         "https://job-boards.greenhouse.io/acme/jobs/1"]
        assert calls[0][0] == "HEAD"

    def test_redirect_loop_is_bounded(self, monkeypatch):
        import requests
        from services import job_ingest_worker as w
        from services import jd_fetcher
        n = {"c": 0}

        def fake(method, url, **kw):
            n["c"] += 1
            return _Resp(302, f"https://boards.greenhouse.io/acme/jobs/{n['c']}")

        monkeypatch.setattr(requests, "request", fake)
        monkeypatch.setattr(jd_fetcher.socket, "getaddrinfo",
                            lambda h, p, **k: [(2, 1, 6, "", ("104.18.1.1", p))])
        import time
        status = w.http_probe_status(
            "greenhouse", "https://boards.greenhouse.io/acme/jobs/1", self._policy(),
            deadline=time.monotonic() + 20)
        assert status is None
        assert n["c"] <= 6

    def test_dns_rebinding_hop_is_refused(self, monkeypatch):
        """A public-looking hop whose name resolves to a private address."""
        import requests
        from services import job_ingest_worker as w
        from services import jd_fetcher
        calls = []

        def fake(method, url, **kw):
            calls.append(url)
            return _Resp(302, "https://rebind.attacker.example/x")

        def resolve(host, port, **kw):
            ip = "10.0.0.5" if host == "rebind.attacker.example" else "104.18.1.1"
            return [(2, 1, 6, "", (ip, port))]

        monkeypatch.setattr(requests, "request", fake)
        monkeypatch.setattr(jd_fetcher.socket, "getaddrinfo", resolve)
        import time
        assert w.http_probe_status(
            "greenhouse", "https://boards.greenhouse.io/acme/jobs/1", self._policy(),
            deadline=time.monotonic() + 20) is None
        assert calls == ["https://boards.greenhouse.io/acme/jobs/1"]
