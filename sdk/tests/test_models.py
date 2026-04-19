"""Tests for Plaidify SDK models."""

from plaidify.models import (
    AccessJobInfo,
    AccessJobListResult,
    ConnectResult,
    BlueprintInfo,
    BlueprintListResult,
    MFAChallenge,
    LinkResult,
    MFASubmitResult,
    AuthToken,
    UserProfile,
    HealthStatus,
)


class TestConnectResult:
    def test_connected_status(self):
        r = ConnectResult(status="connected", job_id="ajob-1", data={"balance": 100.0})
        assert r.connected is True
        assert r.mfa_required is False
        assert r.pending is False
        assert r.job_id == "ajob-1"
        assert r.data == {"balance": 100.0}

    def test_mfa_required_status(self):
        r = ConnectResult(
            status="mfa_required",
            session_id="sess-123",
            mfa_type="otp",
            metadata={"message": "Enter code"},
        )
        assert r.connected is False
        assert r.mfa_required is True
        assert r.session_id == "sess-123"
        assert r.mfa_type == "otp"

    def test_unknown_status(self):
        r = ConnectResult(status="pending")
        assert r.connected is False
        assert r.mfa_required is False
        assert r.pending is True
        assert r.data is None

    def test_immutable(self):
        r = ConnectResult(status="connected")
        try:
            r.status = "changed"
            assert False, "Should be immutable (frozen)"
        except AttributeError:
            pass


class TestBlueprintInfo:
    def test_basic_blueprint(self):
        bp = BlueprintInfo(
            site="greengrid_energy",
            name="GreenGrid Energy",
            domain="greengrid.example.com",
            tags=["utility", "energy"],
            has_mfa=True,
            extract_fields=["current_bill", "usage_history"],
        )
        assert bp.site == "greengrid_energy"
        assert bp.has_mfa is True
        assert len(bp.extract_fields) == 2

    def test_defaults(self):
        bp = BlueprintInfo(site="test", name="Test", domain="test.com")
        assert bp.tags == []
        assert bp.has_mfa is False
        assert bp.extract_fields == []
        assert bp.schema_version == "2"


class TestBlueprintListResult:
    def test_list_result(self):
        bps = [
            BlueprintInfo(site="a", name="A", domain="a.com"),
            BlueprintInfo(site="b", name="B", domain="b.com"),
        ]
        result = BlueprintListResult(blueprints=bps, count=2)
        assert result.count == 2
        assert len(result.blueprints) == 2


class TestMFAChallenge:
    def test_challenge(self):
        c = MFAChallenge(
            session_id="s1",
            site="bank",
            mfa_type="otp",
            metadata={"question": "Enter code"},
        )
        assert c.session_id == "s1"
        assert c.mfa_type == "otp"


class TestAccessJobInfo:
    def test_pending_job(self):
        job = AccessJobInfo(
            job_id="ajob-1",
            site="bank",
            job_type="connect",
            status="running",
        )
        assert job.pending is True
        assert job.completed is False
        assert job.mfa_required is False

    def test_completed_job(self):
        job = AccessJobInfo(
            job_id="ajob-2",
            site="bank",
            job_type="connect",
            status="completed",
            result={"status": "connected", "data": {"balance": 42}},
        )
        assert job.pending is False
        assert job.completed is True
        assert job.result["status"] == "connected"


class TestAccessJobListResult:
    def test_list_result(self):
        jobs = [
            AccessJobInfo(job_id="a1", site="bank", job_type="connect", status="completed"),
            AccessJobInfo(job_id="a2", site="bank", job_type="connect", status="running"),
        ]
        result = AccessJobListResult(jobs=jobs, count=2)
        assert result.count == 2
        assert len(result.jobs) == 2


class TestLinkResult:
    def test_link_created(self):
        link = LinkResult(link_token="lt-123", site="bank")
        assert link.link_token == "lt-123"
        assert link.access_token is None

    def test_link_with_access_token(self):
        link = LinkResult(link_token="lt-123", access_token="at-456")
        assert link.access_token == "at-456"


class TestMFASubmitResult:
    def test_success(self):
        r = MFASubmitResult(status="mfa_submitted", message="Code accepted.")
        assert r.status == "mfa_submitted"

    def test_error(self):
        r = MFASubmitResult(status="error", error="Session expired.")
        assert r.error == "Session expired."


class TestAuthToken:
    def test_token(self):
        t = AuthToken(access_token="jwt-abc")
        assert t.access_token == "jwt-abc"
        assert t.token_type == "bearer"


class TestUserProfile:
    def test_profile(self):
        p = UserProfile(id=1, username="alice", email="alice@example.com")
        assert p.id == 1
        assert p.is_active is True


class TestHealthStatus:
    def test_healthy(self):
        h = HealthStatus(status="healthy", version="0.2.0", database="connected")
        assert h.status == "healthy"
