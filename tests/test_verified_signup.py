"""Email-verified sign-up (REGISTRATION_EMAIL_VERIFICATION).

POST /auth/register answers 202 ``verification_sent`` whether or not the
username or the address is taken, and only the address is told what
happened: a new address asking for a free username is mailed a one-time
token, and POST /auth/verify-email with that token and the password from
registration creates the account; an address that has an account, or one
asking for a taken username, is mailed a note.

Also covers the setting's default per environment, the production startup
check, the purge of expired sign-ups, and both endpoints while registration
is disabled or sign-ups are instant.
"""

import json
import logging
import re
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import src.app as appmod
import src.mailer as mailer
import src.routers.auth as auth_router
from src.database import AuditLog, PendingRegistration, User, utcnow
from tests.conftest import TestSessionLocal

PASSWORD = "Strong@pass123"
PENDING_REPLY = {
    "status": "verification_sent",
    "detail": "If the address can be used, we sent it a link to finish signing up.",
}
INVALID = {"detail": "Invalid or expired verification token"}
UNAVAILABLE = {"detail": "That username or address is no longer available. Register again."}


@pytest.fixture
def outbox():
    """Sign-ups verified, mail configured, and every email the app sends collected instead of sent."""
    sent = []
    with (
        patch.object(auth_router.settings, "registration_email_verification", True),
        patch.object(mailer.settings, "smtp_host", "smtp.example.com"),
        patch.object(mailer.settings, "smtp_from", "Plaidify <no-reply@example.com>"),
        patch.object(
            mailer,
            "send_email",
            side_effect=lambda to, subject, body: sent.append(SimpleNamespace(to=to, subject=subject, body=body)),
        ),
    ):
        yield sent


def _sign_up(client, username, email, password=PASSWORD):
    return client.post("/auth/register", json={"username": username, "email": email, "password": password})


def _verify(client, token, password=PASSWORD):
    return client.post("/auth/verify-email", json={"token": token, "password": password})


def _code(mail) -> str:
    """The one-time code a verification email carries (no EMAIL_VERIFICATION_URL set)."""
    return re.search(r"finish signing up:\n\n    (\S+)\n", mail.body).group(1)


def _add_account(username, email):
    with TestSessionLocal() as db:
        db.add(User(username=username, email=email, hashed_password="not-a-bcrypt-hash"))
        db.commit()


def _pending():
    with TestSessionLocal() as db:
        return [(p.username, p.email) for p in db.query(PendingRegistration)]


class TestUniformReply:
    def test_a_new_or_taken_address_or_username_gets_the_same_reply(self, client, outbox):
        _add_account("alice", "alice@example.com")

        replies = [
            _sign_up(client, "bob", "bob@example.com"),  # new address, free username
            _sign_up(client, "carol", "alice@example.com"),  # the address has an account
            _sign_up(client, "alice", "dave@example.com"),  # new address, taken username
        ]

        assert [(r.status_code, r.json()) for r in replies] == [(202, PENDING_REPLY)] * 3
        with TestSessionLocal() as db:
            assert [u.username for u in db.query(User)] == ["alice"]
        assert _pending() == [("bob", "bob@example.com")]

        verification, address_in_use, username_taken = outbox
        assert (verification.to, verification.subject) == ("bob@example.com", "Finish signing up for Plaidify")
        assert 'the username "bob"' in verification.body and _code(verification)
        assert address_in_use.to == "alice@example.com"
        assert "already has an account" in address_in_use.body
        assert username_taken.to == "dave@example.com"
        assert "the username they chose is taken" in username_taken.body
        assert "one-time code" not in address_in_use.body + username_taken.body

    def test_nothing_that_depends_on_what_is_taken_runs_before_the_reply(self, client, outbox):
        _add_account("alice", "alice@example.com")
        with patch.object(auth_router, "_start_sign_up") as start:
            new = _sign_up(client, "bob", "bob@example.com")
            taken = _sign_up(client, "carol", "alice@example.com")

        assert (new.status_code, new.json()) == (taken.status_code, taken.json()) == (202, PENDING_REPLY)
        # The lookups, the pending row and the mail are all left to the task run after the reply.
        assert [c.args[:2] for c in start.call_args_list] == [
            ("bob", "bob@example.com"),
            ("carol", "alice@example.com"),
        ]
        assert outbox == [] and _pending() == []

    def test_every_sign_up_costs_one_password_hash(self, client, outbox):
        _add_account("alice", "alice@example.com")
        with patch.object(auth_router, "get_password_hash", wraps=auth_router.get_password_hash) as hashed:
            _sign_up(client, "bob", "bob@example.com")
            _sign_up(client, "carol", "alice@example.com")
            _sign_up(client, "alice", "dave@example.com")
        assert hashed.call_count == 3

    def test_invalid_input_is_still_422(self, client, outbox):
        assert _sign_up(client, "bob", "not-an-email").status_code == 422
        assert _sign_up(client, "bob", "bob@example.com", password="short").status_code == 422
        assert outbox == []


class TestVerifyEmail:
    def test_the_mailed_token_creates_the_account_and_signs_it_in(self, client, outbox):
        _sign_up(client, "bob", "bob@example.com")

        resp = _verify(client, _code(outbox[0]))

        assert resp.status_code == 200
        tokens = resp.json()
        assert tokens["token_type"] == "bearer" and tokens["refresh_token"]
        me = client.get("/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}).json()
        assert (me["username"], me["email"]) == ("bob", "bob@example.com")
        with TestSessionLocal() as db:
            user = db.query(User).one()
            assert user.email_verified and user.encrypted_dek
            entry = db.query(AuditLog).filter(AuditLog.action == "register").one()
            assert entry.user_id == user.id and json.loads(entry.metadata_json) == {"username": "bob"}
        assert _pending() == []
        # The account has the password chosen at sign-up, presented again here.
        assert client.post("/auth/token", data={"username": "bob", "password": PASSWORD}).status_code == 200

    def test_the_token_alone_or_the_wrong_password_does_not_create_the_account(self, client, outbox):
        _sign_up(client, "bob", "bob@example.com")
        token = _code(outbox[0])

        missing = client.post("/auth/verify-email", json={"token": token})
        wrong = _verify(client, token, "Wrong@pass123")

        assert missing.status_code == 422
        assert (wrong.status_code, wrong.json()) == (400, INVALID)
        # Neither attempt spends the token or creates the account.
        assert _pending() == [("bob", "bob@example.com")]
        assert client.post("/auth/token", data={"username": "bob", "password": PASSWORD}).status_code == 400
        assert _verify(client, token).status_code == 200

    def test_unknown_used_and_expired_tokens_are_refused_alike(self, client, outbox):
        _sign_up(client, "bob", "bob@example.com")
        _sign_up(client, "erin", "erin@example.com")
        bob, erin = (_code(mail) for mail in outbox)
        assert _verify(client, bob).status_code == 200
        with TestSessionLocal() as db:
            db.query(PendingRegistration).update({"expires_at": utcnow() - timedelta(seconds=1)})
            db.commit()

        refused = [_verify(client, token) for token in ("not-a-token", bob, erin)]

        assert [(r.status_code, r.json()) for r in refused] == [(400, INVALID)] * 3
        with TestSessionLocal() as db:
            assert [u.username for u in db.query(User)] == ["bob"]

    def test_a_username_taken_before_verification_voids_the_sign_up(self, client, outbox):
        # A pending sign-up holds no username: the first of the two to be verified gets it.
        _sign_up(client, "frank", "frank1@example.com")
        _sign_up(client, "frank", "frank2@example.com")
        first, second = (_code(mail) for mail in outbox)
        assert _verify(client, second).status_code == 200

        resp = _verify(client, first)

        assert (resp.status_code, resp.json()) == (409, UNAVAILABLE)
        assert _verify(client, first).status_code == 400
        with TestSessionLocal() as db:
            assert [(u.username, u.email) for u in db.query(User)] == [("frank", "frank2@example.com")]

    def test_a_username_taken_at_the_same_moment_ends_the_same_way(self, client, outbox):
        _sign_up(client, "grace", "grace@example.com")
        _add_account("grace", "someone-else@example.com")

        # Taken between the check and the insert: the unique constraint refuses the account.
        with patch.object(auth_router, "_holders_of", return_value=[]):
            resp = _verify(client, _code(outbox[0]))

        assert (resp.status_code, resp.json()) == (409, UNAVAILABLE)
        assert _verify(client, _code(outbox[0])).status_code == 400
        assert _pending() == []

    def test_a_new_sign_up_for_the_address_replaces_the_earlier_one(self, client, outbox):
        _sign_up(client, "henry", "henry@example.com")
        _sign_up(client, "henry2", "henry@example.com", password="Other@pass456")
        earlier, later = (_code(mail) for mail in outbox)

        assert _pending() == [("henry2", "henry@example.com")]
        assert _verify(client, earlier).status_code == 400
        assert _verify(client, later, "Other@pass456").status_code == 200
        assert client.post("/auth/token", data={"username": "henry2", "password": "Other@pass456"}).status_code == 200

    def test_the_address_is_kept_as_the_users_table_keeps_it(self, client, outbox):
        _sign_up(client, "liz", "Liz@Example.COM")
        _sign_up(client, "liz", "Liz@EXAMPLE.com")  # the same address: replaces the first

        assert _pending() == [("liz", "Liz@example.com")]
        assert _verify(client, _code(outbox[-1])).status_code == 200
        with TestSessionLocal() as db:
            assert db.query(User.email).scalar() == "Liz@example.com"

    def test_the_email_links_to_the_sign_up_page_when_one_is_set(self, client, outbox):
        with patch.object(mailer.settings, "email_verification_url", "https://app.example.com/verify?token={token}"):
            _sign_up(client, "ivy", "ivy@example.com")

        link = re.search(r"https://app\.example\.com/verify\?token=(\S+)", outbox[0].body)
        assert link and "one-time code" not in outbox[0].body
        assert _verify(client, link.group(1)).status_code == 200


class TestDelivery:
    def test_without_smtp_the_sign_up_is_logged_as_undeliverable(self, client, caplog):
        with (
            patch.object(auth_router.settings, "registration_email_verification", True),
            patch.object(mailer.settings, "smtp_host", None),
            caplog.at_level(logging.WARNING),
        ):
            resp = _sign_up(client, "jack", "jack@example.com")

        assert (resp.status_code, resp.json()) == (202, PENDING_REPLY)
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("sign-up emails are disabled" in message for message in warnings)
        assert not any("jack" in r.getMessage() for r in caplog.records)

    def test_a_failed_delivery_is_logged_and_the_reply_is_unchanged(self, client, outbox, caplog):
        with (
            patch.object(mailer, "send_email", side_effect=OSError("connection refused")),
            caplog.at_level(logging.ERROR),
        ):
            resp = _sign_up(client, "kate", "kate@example.com")

        assert (resp.status_code, resp.json()) == (202, PENDING_REPLY)
        assert any(r.getMessage() == "Sign-up email could not be sent" for r in caplog.records)


class TestWhenOff:
    def test_registration_disabled_answers_403_on_both_endpoints(self, client, outbox):
        with patch.object(auth_router.settings, "registration_enabled", False):
            assert _sign_up(client, "bob", "bob@example.com").status_code == 403
            assert _verify(client, "a-token").status_code == 403
        assert outbox == [] and _pending() == []

    def test_verify_email_is_404_while_sign_ups_are_instant(self, client):
        # Outside production verification is off unless set: register creates the account at once.
        assert auth_router.settings.registration_email_verification is False
        assert _verify(client, "a-token").status_code == 404
        assert "access_token" in _sign_up(client, "bob", "bob@example.com").json()


def test_verify_email_is_rate_limited_like_register(client):
    from src.dependencies import limiter

    limiter.enabled = True  # the autouse fixture turns it off again with fresh storage
    assert [_verify(client, "a-token").status_code for _ in range(4)] == [404, 404, 404, 429]


def test_expired_sign_ups_are_purged():
    now = utcnow()
    with TestSessionLocal() as db:
        db.add_all(
            [
                PendingRegistration(
                    username="old",
                    email="old@example.com",
                    hashed_password="h",
                    token_hash="a" * 64,
                    expires_at=now - timedelta(minutes=1),
                ),
                PendingRegistration(
                    username="new",
                    email="new@example.com",
                    hashed_password="h",
                    token_hash="b" * 64,
                    expires_at=now + timedelta(hours=1),
                ),
            ]
        )
        db.commit()

    appmod._purge_expired_auth_rows()

    assert _pending() == [("new", "new@example.com")]


# ── The setting ───────────────────────────────────────────────────────────────

_SETTINGS = {
    "encryption_key": "s790nQg9kGoAVQGqXreKUbG8Q0OA-A4HASTbyd-ruuQ=",
    "jwt_secret_key": "test-secret-key-for-testing-only-not-production",
    "database_url": "postgresql://u:p@db/plaidify",
    "_env_file": None,
}


def _settings(monkeypatch, **overrides):
    from src.config import Settings

    for name in ("ENV", "REGISTRATION_ENABLED", "REGISTRATION_EMAIL_VERIFICATION"):
        monkeypatch.delenv(name, raising=False)
    return Settings(**{**_SETTINGS, **overrides})


class TestSettingDefault:
    @pytest.mark.parametrize(("env", "verified"), [("development", False), ("staging", False), ("production", True)])
    def test_on_in_production_only_unless_set(self, monkeypatch, env, verified):
        assert _settings(monkeypatch, env=env).registration_email_verification is verified

    @pytest.mark.parametrize("env", ["development", "production"])
    @pytest.mark.parametrize("value", [True, False])
    def test_a_set_value_wins(self, monkeypatch, env, value):
        settings = _settings(monkeypatch, env=env, registration_email_verification=value)
        assert settings.registration_email_verification is value

    def test_an_empty_variable_means_unset(self, monkeypatch):
        from src.config import Settings

        _settings(monkeypatch)
        monkeypatch.setenv("ENV", "production")
        monkeypatch.setenv("REGISTRATION_EMAIL_VERIFICATION", "")
        assert Settings(**_SETTINGS).registration_email_verification is True
        monkeypatch.setenv("REGISTRATION_EMAIL_VERIFICATION", "false")
        assert Settings(**_SETTINGS).registration_email_verification is False


class TestProductionStartup:
    @pytest.fixture
    def production(self):
        """Production with open, verified sign-ups and no mail; every other startup check passes."""
        with (
            patch.object(appmod.settings, "env", "production"),
            patch.object(appmod.settings, "debug", False),
            patch.object(appmod.settings, "redis_url", "redis://redis:6379/0"),
            patch.object(appmod, "_get_redis", return_value=MagicMock()),
            patch.object(appmod.settings, "registration_enabled", True),
            patch.object(appmod.settings, "registration_email_verification", True),
            patch.object(mailer.settings, "smtp_host", None),
            patch.object(mailer.settings, "smtp_from", None),
        ):
            yield

    def test_verified_sign_up_without_mail_stops_startup(self, production):
        with pytest.raises(RuntimeError, match="SMTP_HOST/SMTP_FROM are not set") as refused:
            appmod._validate_runtime_configuration()
        assert "REGISTRATION_EMAIL_VERIFICATION=false" in str(refused.value)

    def test_mail_the_opt_out_or_closed_registration_let_it_start(self, production):
        with (
            patch.object(mailer.settings, "smtp_host", "smtp.example.com"),
            patch.object(mailer.settings, "smtp_from", "no-reply@example.com"),
        ):
            appmod._validate_runtime_configuration()
        with patch.object(appmod.settings, "registration_email_verification", False):
            appmod._validate_runtime_configuration()
        with patch.object(appmod.settings, "registration_enabled", False):
            appmod._validate_runtime_configuration()
