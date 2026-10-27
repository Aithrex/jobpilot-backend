"""A job dated today reads "Today" in every timezone.

Reported 2026-10-26 from Phoenix (UTC-7): jobs the employer's board showed as
posted today only appeared under "Since yesterday". Every date-only posting is
stored as midnight UTC (`ats_providers.util.to_iso`, Oracle ORC, NeoGov), and
`_days_ago` read that as an INSTANT - 5pm the previous day in Phoenix - so it
was one day old for the viewer's whole day. Every viewer west of UTC saw it.

These tests freeze the clock, because the bug lives at day boundaries.
"""
from datetime import datetime, timedelta, timezone

import pytest

from services import inventory_feed
from services import workday_jobs_service as wjs

PHOENIX = -420
US_OFFSETS = (-240, -300, -360, -420, -480, -540, -600)   # EDT .. HST
EAST_OFFSETS = (0, 60, 330, 345, 480, 540, 600, 780, 840)  # UTC .. UTC+14


def _freeze(monkeypatch, now: datetime) -> None:
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is None else now.astimezone(tz)

    monkeypatch.setattr(wjs, "datetime", _Frozen)


def _instant_age(iso: str, offset_minutes: int, now: datetime) -> int:
    """The pre-fix reading: the stored value as an instant, in the viewer frame."""
    d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    off = timedelta(minutes=offset_minutes)
    return max(0, ((now + off).date() - (d.astimezone(timezone.utc) + off).date()).days)


def _local_now(offset_minutes: int, hour: int, minute: int = 0) -> datetime:
    """The UTC instant at which a viewer at `offset_minutes` reads 2026-10-26 hour:minute."""
    local = datetime(2026, 10, 26, hour, minute, tzinfo=timezone(timedelta(minutes=offset_minutes)))
    return local.astimezone(timezone.utc)


# --- the reported bug -------------------------------------------------------

def test_a_job_dated_today_is_today_in_phoenix_all_evening(monkeypatch):
    # 19:22 MST on Oct 26 is already Oct 27 in UTC - the reporter's own clock.
    _freeze(monkeypatch, datetime(2026, 10, 27, 2, 22, tzinfo=timezone.utc))
    assert wjs._days_ago("2026-10-26T00:00:00+00:00", PHOENIX) == 0
    assert wjs._days_ago("2026-10-25T00:00:00+00:00", PHOENIX) == 1
    assert wjs._days_ago("2026-10-23T00:00:00+00:00", PHOENIX) == 3


@pytest.mark.parametrize("offset", US_OFFSETS)
@pytest.mark.parametrize("hour", [0, 8, 13, 17, 23])
def test_a_job_dated_today_is_today_at_every_hour_in_every_us_zone(monkeypatch, offset, hour):
    _freeze(monkeypatch, _local_now(offset, hour, 30))
    assert wjs._days_ago("2026-10-26T00:00:00+00:00", offset) == 0
    assert wjs._days_ago("2026-10-25T00:00:00+00:00", offset) == 1
    assert wjs._days_ago("2026-10-24T00:00:00+00:00", offset) == 2


@pytest.mark.parametrize("stored", [
    "2026-10-26T00:00:00+00:00",          # to_iso / Oracle ORC / NeoGov
    "2026-10-26T00:00:00Z",
    "2026-10-26T00:00:00",                # naive, read as UTC
    "2026-10-26T00:00:00-04:00",          # vendor's own local midnight
    "2026-10-26T04:00:00.000Z",           # iCIMS: midnight EDT published as UTC
    "2026-10-26T07:00:00+00:00",          # midnight MST published as UTC
    "2026-10-26T03:30:00+00:00",          # midnight Newfoundland
    "2026-10-26",                         # bare date (legacy rows)
])
def test_every_date_encoding_reads_as_its_date(monkeypatch, stored):
    _freeze(monkeypatch, _local_now(PHOENIX, 9))
    assert wjs._days_ago(stored, PHOENIX) == 0


# --- real instants keep their meaning ---------------------------------------

def test_a_real_instant_from_last_night_is_still_yesterday(monkeypatch):
    """Seconds on the stamp mean a real moment, not a date: 01:15:42Z is
    18:15 the previous evening in Phoenix, and that stays one day old."""
    _freeze(monkeypatch, _local_now(PHOENIX, 9))
    assert wjs._days_ago("2026-10-26T01:15:42+00:00", PHOENIX) == 1
    assert wjs._days_ago("2026-10-26T01:15:42.123000+00:00", PHOENIX) == 1


def test_a_freshly_crawled_posting_is_today(monkeypatch):
    now = _local_now(PHOENIX, 9)
    _freeze(monkeypatch, now)
    stamp = (now - timedelta(hours=2, seconds=7, microseconds=431000)).isoformat()
    assert wjs._days_ago(stamp, PHOENIX) == 0


def test_an_afternoon_on_the_hour_post_is_not_moved_forward(monkeypatch):
    """15:00Z sits outside the Western-Hemisphere midnight band, so an
    on-the-hour daytime post is read as the instant it is."""
    _freeze(monkeypatch, _local_now(PHOENIX, 9))
    assert wjs._days_ago("2026-10-25T15:00:00+00:00", PHOENIX) == 1


# --- no viewer is made worse --------------------------------------------------

@pytest.mark.parametrize("offset", EAST_OFFSETS)
@pytest.mark.parametrize("hour", [0, 6, 12, 18, 23])
def test_viewers_at_or_east_of_utc_are_unchanged(monkeypatch, offset, hour):
    now = _local_now(offset, hour, 15)
    _freeze(monkeypatch, now)
    for stored in ("2026-10-26T00:00:00+00:00", "2026-10-25T00:00:00+00:00",
                   "2026-10-26T04:00:00+00:00", "2026-10-25T09:00:00+00:00",
                   "2026-10-25T18:42:10+00:00"):
        assert wjs._days_ago(stored, offset) == _instant_age(stored, offset, now), stored


@pytest.mark.parametrize("offset", US_OFFSETS + EAST_OFFSETS + (-660, -720))
def test_no_posting_ever_reads_older_than_before(monkeypatch, offset):
    """The date reading may only rescue a row, never age one - so a row can
    never leave a recency window it used to be in."""
    now = _local_now(offset, 14)
    _freeze(monkeypatch, now)
    base = datetime(2026, 10, 20, tzinfo=timezone.utc)
    for half_hours in range(0, 7 * 48):
        stamp = (base + timedelta(minutes=30 * half_hours)).isoformat()
        assert wjs._days_ago(stamp, offset) <= _instant_age(stamp, offset, now), stamp


def test_a_future_date_is_today_not_negative(monkeypatch):
    _freeze(monkeypatch, _local_now(PHOENIX, 9))
    assert wjs._days_ago("2026-10-27T00:00:00+00:00", PHOENIX) == 0


@pytest.mark.parametrize("bad", ["", None, "not a date", "2026-13-45T00:00:00Z"])
def test_unparseable_stays_unknown(bad):
    assert wjs._days_ago(bad, PHOENIX) is None


# --- the row the For-you feed renders ---------------------------------------

def test_the_feed_row_says_today_for_a_date_only_posting(monkeypatch):
    _freeze(monkeypatch, datetime(2026, 10, 27, 2, 22, tzinfo=timezone.utc))
    doc = {"job_id": "j1", "title_raw": "Software Engineer", "employer_name": "Acme",
           "locations": [{"raw": "Phoenix, AZ", "country_code": "US"}],
           "status": "active", "source": "career-pages", "ats": "oracle",
           "posted_at": datetime(2026, 10, 26, tzinfo=timezone.utc),
           "posted_at_confidence": "exact"}
    row = inventory_feed._row_from_posting(doc, tz_offset=PHOENIX)
    assert row["days_ago"] == 0
    assert row["posted_text"] == "Today"
