"""Pieces the Oracle box runs instead of AWS: vault cipher, PII masking,
presigned host, scheduler slots."""
import base64
import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from services import kms_cipher
from services.kms_cipher import KmsCipherError, LocalEnvelopeCipher, credential_cipher
from services.llm_providers.pii_mask import PiiMasker


@pytest.fixture()
def master_key(monkeypatch):
    monkeypatch.setenv("CREDENTIALS_MASTER_KEY", base64.b64encode(b"k" * 32).decode())


def test_local_cipher_round_trip_and_context_binding(master_key):
    cipher = LocalEnvelopeCipher()
    blob = cipher.encrypt("hunter2", context={"user_email": "a@x.com"})
    assert blob["v"] == 2
    assert "hunter2" not in str(blob)
    assert cipher.decrypt(blob, context={"user_email": "a@x.com"}) == "hunter2"
    with pytest.raises(KmsCipherError):
        cipher.decrypt(blob, context={"user_email": "b@x.com"})


def test_local_cipher_refuses_a_wrong_or_short_key(master_key, monkeypatch):
    blob = LocalEnvelopeCipher().encrypt("x")
    monkeypatch.setenv("CREDENTIALS_MASTER_KEY", base64.b64encode(b"z" * 32).decode())
    with pytest.raises(KmsCipherError):
        LocalEnvelopeCipher().decrypt(blob)
    monkeypatch.setenv("CREDENTIALS_MASTER_KEY", base64.b64encode(b"short").decode())
    with pytest.raises(KmsCipherError):
        LocalEnvelopeCipher().encrypt("x")


def test_cipher_selection(monkeypatch, master_key):
    assert isinstance(credential_cipher(), LocalEnvelopeCipher)
    monkeypatch.delenv("CREDENTIALS_MASTER_KEY")
    assert isinstance(credential_cipher(), kms_cipher.KmsEnvelopeCipher)


def test_masking_hides_contact_details_and_restores_them():
    text = ("Jane Doe | jane.doe@example.com | +1 (415) 555-2671 | 98765 43210 | "
            "linkedin.com/in/janedoe | https://github.com/janedoe | SSN 123-45-6789")
    m = PiiMasker()
    masked = m.mask(text)
    for secret in ("jane.doe@", "555-2671", "98765", "janedoe", "123-45-6789"):
        assert secret not in masked
    assert m.unmask(masked) == text
    assert m.unmask({"a": [masked]}) == {"a": [text]}


@pytest.mark.parametrize("keep", [
    "2019-2023", "Jan 2019 - Mar 2023", "salary 120000", "3.8 GPA", "increased by 45%",
    "id 123456", "ISO 27001", "req 2024-10231",
])
def test_masking_leaves_ordinary_numbers_alone(keep):
    assert PiiMasker().mask(keep) == keep


def test_presigned_urls_use_the_public_host(monkeypatch):
    from services.s3_service import ResumeStorageService
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "k")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "s")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("RESUME_S3_BUCKET", "bkt")
    monkeypatch.setenv("AWS_ENDPOINT_URL_S3", "http://s3:7070")
    monkeypatch.setenv("S3_PUBLIC_ENDPOINT_URL", "https://files.example.com")
    svc = ResumeStorageService()
    assert svc.get_presigned_url("u/a.pdf").startswith("https://files.example.com/bkt/u/a.pdf?")


def _runner():
    path = Path(__file__).resolve().parents[1] / "services" / "scheduler" / "runner.py"
    spec = importlib.util.spec_from_file_location("scheduler_runner", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["scheduler_runner"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_scheduler_slots_match_the_old_rules():
    r = _runner()
    now = datetime(2026, 11, 1, 4, 37, 12, tzinfo=timezone.utc)
    assert r.latest_slot("learning_due", now) == now.replace(minute=30, second=0)
    assert r.latest_slot("digest_hourly", now) == now.replace(minute=0, second=0)
    assert r.latest_slot("ats_signal_aggregate", now) == now.replace(minute=30, second=0)
    assert r.latest_slot("guest_retention", now).day == 31


def test_scheduler_claims_a_slot_exactly_once(mock_db):
    r = _runner()
    coll = mock_db["scheduler_slots"]
    slot = datetime(2026, 11, 1, 4, 0, tzinfo=timezone.utc)
    assert r.claim(coll, "digest_hourly", slot) is True
    assert r.claim(coll, "digest_hourly", slot) is False
    assert r.claim(coll, "digest_hourly", slot.replace(hour=5)) is True


class _FakeModels:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def generate_content(self, model, contents, config):
        self.calls.append((model, contents, config))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return type("R", (), {"text": reply, "candidates": []})()


def _gemini(replies):
    from services.llm_providers.gemini import GeminiProvider
    provider = GeminiProvider()
    fake = _FakeModels(replies)
    provider._client = type("C", (), {"models": fake})()
    return provider, fake


def test_gemini_never_sees_contact_details_and_the_caller_gets_them_back(monkeypatch):
    provider, fake = _gemini(['{"email": "[EMAIL_1]"}'])
    out = provider.json("Parse: jane@example.com", model="gemini-3.8-flash")
    assert out == {"email": "jane@example.com"}
    assert "jane@example.com" not in str(fake.calls[0][1])


def test_gemini_extracted_pdf_is_masked_and_not_sent_as_bytes(monkeypatch):
    from services.llm_providers.gemini import GeminiProvider
    monkeypatch.setattr(GeminiProvider, "_extract_text", staticmethod(
        lambda data, mime: "Jane jane@example.com, ten years of experience. " * 3))
    provider, fake = _gemini(['{"email": "[EMAIL_1]"}'])
    parts = provider.build_pdf_parts("Parse this", b"%PDF-fake")
    assert len(parts) == 1 and "jane@example.com" not in parts[0].text
    assert provider.json(parts=parts, model="gemini-3.8-flash") == {"email": "jane@example.com"}


def test_gemini_moves_to_the_next_model_when_one_is_out_of_quota(monkeypatch):
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "gemini-2.5-flash")
    quota = RuntimeError("429 RESOURCE_EXHAUSTED quota")
    provider, fake = _gemini([quota, '{"ok": "1"}'])
    assert provider.json("hi", model="gemini-3.8-flash", max_retries=0) == {"ok": "1"}
    assert [c[0] for c in fake.calls] == ["gemini-3.8-flash", "gemini-2.5-flash"]


def test_flash_models_get_no_thinking_budget_to_eat_the_output():
    provider, fake = _gemini(["{}"])
    provider.json("hi", model="gemini-3.8-flash")
    assert fake.calls[0][2].thinking_config.thinking_budget == 0
