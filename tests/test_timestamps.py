"""
JOB-23: timestamps are stored zone-aware and always read back as aware UTC.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from src.database import (
    AccessToken,
    Link,
    RefreshToken,
    ScheduledRefreshJob,
    User,
    as_utc,
    utcnow,
)
from tests.conftest import TestSessionLocal

PLUS_FIVE = timezone(timedelta(hours=5))


def _user(db, username="tzuser"):
    user = User(username=username, email=f"{username}@example.com", hashed_password="x")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


class TestHelpers:
    def test_utcnow_is_aware_utc(self):
        now = utcnow()
        assert now.tzinfo is not None
        assert now.utcoffset() == timedelta(0)

    def test_as_utc(self):
        naive = datetime(2026, 1, 2, 3, 4, 5)
        assert as_utc(naive) == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        shifted = datetime(2026, 1, 2, 8, 4, 5, tzinfo=PLUS_FIVE)
        assert as_utc(shifted) == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        assert as_utc(shifted).tzinfo == timezone.utc
        assert as_utc(None) is None


class TestColumns:
    def test_values_come_back_aware_utc(self):
        db = TestSessionLocal()
        try:
            user = _user(db)
            db.expire_all()
            reloaded = db.get(User, user.id)
            assert reloaded.created_at.tzinfo is not None
            assert reloaded.created_at.utcoffset() == timedelta(0)
        finally:
            db.close()

    def test_non_utc_input_is_stored_as_the_same_instant(self):
        db = TestSessionLocal()
        try:
            user = _user(db)
            user.locked_until = datetime(2026, 5, 1, 17, 0, tzinfo=PLUS_FIVE)
            db.commit()
            db.expire_all()
            assert db.get(User, user.id).locked_until == datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        finally:
            db.close()

    def test_naive_input_is_taken_as_utc(self):
        db = TestSessionLocal()
        try:
            user = _user(db)
            user.locked_until = datetime(2026, 5, 1, 12, 0)
            db.commit()
            db.expire_all()
            assert db.get(User, user.id).locked_until == datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
        finally:
            db.close()

    def test_sql_comparisons_use_the_instant(self):
        db = TestSessionLocal()
        try:
            user = _user(db)
            db.add(RefreshToken(token="a", user_id=user.id, expires_at=utcnow() + timedelta(hours=1)))
            db.add(RefreshToken(token="b", user_id=user.id, expires_at=utcnow() - timedelta(hours=1)))
            db.commit()
            # "now" expressed in +05:00 must select the same row as in UTC.
            now_plus_five = utcnow().astimezone(PLUS_FIVE)
            live = db.query(RefreshToken).filter(RefreshToken.expires_at > now_plus_five).all()
            assert len(live) == 1
            assert live[0].expires_at > utcnow()
        finally:
            db.close()

    def test_api_timestamps_carry_an_offset(self, client, auth_headers):
        sessions = client.get("/auth/sessions", headers=auth_headers).json()["sessions"]
        assert sessions and sessions[0]["created_at"].endswith("+00:00")


class TestScheduledRefreshAfterRestart:
    """The call site that broke: jobs reloaded from the database compared naive with aware."""

    def test_reloaded_job_can_be_checked_for_due(self):
        from src.scheduled_refresh import RefreshScheduler

        db = TestSessionLocal()
        try:
            user = _user(db, "refresher")
            db.add(Link(link_token="lt", site="s", user_id=user.id))
            db.add(
                AccessToken(
                    token="at", link_token="lt", username_encrypted="u", password_encrypted="p", user_id=user.id
                )
            )
            db.add(
                ScheduledRefreshJob(
                    access_token="at",
                    user_id=user.id,
                    interval_seconds=60,
                    last_refreshed=utcnow() - timedelta(minutes=5),
                )
            )
            db.commit()
        finally:
            db.close()

        scheduler = RefreshScheduler(fetch_callback=AsyncMock(), interval_seconds=60)
        assert scheduler.load_from_db() == 1
        job = scheduler._jobs["at"]
        assert job.last_refreshed.tzinfo is not None
        assert scheduler._is_due(job, datetime.now(timezone.utc)) is True
