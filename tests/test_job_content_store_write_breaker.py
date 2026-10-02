"""A denied S3 write is billed exactly like a successful one.

Measured 2026-09: the EC2 ingest box's role had no s3:PutObject on `job-jd/*`,
so every crawl tick did HEAD (miss) + PUT (403) for every posting it saw, and
nothing was remembered because nothing succeeded. 1.99M PutObject requests in
September, ~90% of the S3 bill, and not one JD body actually stored.

The store must stop asking once it has been told no.
"""
import pytest
from botocore.exceptions import ClientError

import services.job_content_store as jcs


def _err(code, op):
    return ClientError({"Error": {"Code": code, "Message": code}}, op)


class FakeS3:
    def __init__(self, put_error=None):
        self.put_error = put_error
        self.heads = 0
        self.puts = 0

    def head_object(self, **kw):
        self.heads += 1
        raise _err("404", "HeadObject")

    def put_object(self, **kw):
        self.puts += 1
        if self.put_error:
            raise _err(self.put_error, "PutObject")


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setenv("RESUME_S3_BUCKET", "test-bucket")
    jcs.reset_caches()

    def install(fake):
        monkeypatch.setattr(jcs, "_client", fake)
        return fake

    yield install
    jcs.reset_caches()


H1 = "a" * 64
H2 = "b" * 64


def test_access_denied_pauses_further_writes(store):
    s3 = store(FakeS3(put_error="AccessDenied"))
    assert jcs.put_jd(H1, "body one") is None
    assert (s3.heads, s3.puts) == (1, 1)

    # Every later call is free: no HEAD, no PUT, body stays inline in Mongo.
    for h in (H1, H2, "c" * 64):
        assert jcs.put_jd(h, "body") is None
    assert (s3.heads, s3.puts) == (1, 1)


def test_archive_is_paused_too_and_reports_failure(store):
    s3 = store(FakeS3(put_error="AccessDenied"))
    jcs.put_jd(H1, "body")
    assert jcs.archive_posting({"job_id": "1234abcd", "description_text": "x"}) is False
    assert s3.puts == 1  # the archive never reached S3


def test_writes_resume_after_the_pause(store, monkeypatch):
    s3 = store(FakeS3(put_error="AccessDenied"))
    jcs.put_jd(H1, "body")
    s3.put_error = None
    monkeypatch.setattr(jcs, "_write_paused_until", 0.0)
    assert jcs.put_jd(H2, "body two") == H2
    assert s3.puts == 2


def test_a_transient_error_does_not_pause(store):
    s3 = store(FakeS3(put_error="SlowDown"))
    assert jcs.put_jd(H1, "body") is None
    s3.put_error = None
    assert jcs.put_jd(H2, "body") == H2
    assert s3.puts == 2


def test_a_successful_write_is_remembered(store):
    s3 = store(FakeS3())
    assert jcs.put_jd(H1, "body") == H1
    assert jcs.put_jd(H1, "body") == H1
    assert (s3.heads, s3.puts) == (1, 1)
