"""
Tests for the Blueprint Auto-Generator module.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.blueprint import BlueprintV2, ExtractionStrategy
from src.core.blueprint_generator import (
    BlueprintGenerator,
    GeneratedBlueprint,
    LoginFormDiscovery,
    _domain_to_site_key,
)
from src.core.llm_provider import LLMProviderError, LLMResponse, TokenUsage

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_llm_response(content: dict, **kwargs) -> LLMResponse:
    """Create an LLMResponse with JSON content."""
    return LLMResponse(
        content=json.dumps(content),
        model=kwargs.get("model", "gpt-4o-mini"),
        usage=TokenUsage(100, 50, 150),
        latency_ms=200.0,
        provider="openai",
    )


def _make_mock_provider(login_response: dict, field_response: dict) -> MagicMock:
    """Create a mock LLM provider that returns login discovery then field discovery."""
    provider = MagicMock()
    provider.provider_name = "openai"

    # extract() is called twice: once for DOM login discovery, once for field discovery
    provider.extract = AsyncMock(
        side_effect=[
            _make_llm_response(login_response),
            _make_llm_response(field_response),
        ]
    )
    provider.close = AsyncMock()
    return provider


def _make_mock_page(
    html: str = "<html><body><form><input id='user'><input id='pass'><button>Login</button></form></body></html>",
) -> MagicMock:
    """Create a mock Playwright page."""
    page = MagicMock()
    page.content = AsyncMock(return_value=html)
    page.evaluate = AsyncMock(return_value=html)
    page.screenshot = AsyncMock(return_value=b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    page.viewport_size = {"width": 1280, "height": 720}
    page.set_viewport_size = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    return page


# ── Domain to Site Key Tests ──────────────────────────────────────────────────


class TestDomainToSiteKey:
    def test_simple_domain(self):
        assert _domain_to_site_key("example.com") == "example_com"

    def test_subdomain(self):
        assert _domain_to_site_key("login.mybank.com") == "login_mybank_com"

    def test_domain_with_port(self):
        assert _domain_to_site_key("localhost:8080") == "localhost"

    def test_domain_with_hyphens(self):
        assert _domain_to_site_key("my-cool-bank.co.uk") == "my_cool_bank_co_uk"

    def test_empty_returns_unknown(self):
        assert _domain_to_site_key("") == "unknown_site"

    def test_uppercase_lowered(self):
        assert _domain_to_site_key("MyBank.COM") == "mybank_com"


# ── LoginFormDiscovery Tests ─────────────────────────────────────────────────


class TestLoginFormDiscovery:
    def test_dataclass(self):
        d = LoginFormDiscovery(
            username_selector="#user",
            password_selector="#pass",
            submit_selector="#submit",
            pre_login_steps=[],
            mfa_likely=False,
            mfa_hints="",
            page_title="My Bank",
            confidence=0.9,
        )
        assert d.username_selector == "#user"
        assert d.confidence == 0.9


# ── Blueprint Generator Core Tests ───────────────────────────────────────────


class TestBlueprintGenerator:
    """Test the BlueprintGenerator with mocked LLM and browser."""

    @pytest.fixture
    def login_response(self):
        return {
            "username_selector": "#email",
            "password_selector": "#password",
            "submit_selector": "button[type='submit']",
            "pre_login_steps": [],
            "mfa_likely": False,
            "mfa_hints": "",
            "page_title": "Acme Bank Login",
            "confidence": 0.85,
        }

    @pytest.fixture
    def field_response(self):
        return {
            "fields": [
                {
                    "name": "account_balance",
                    "type": "currency",
                    "description": "Current account balance",
                    "example": "$5,432.10",
                    "sensitive": False,
                },
                {
                    "name": "account_number",
                    "type": "text",
                    "description": "Account number",
                    "sensitive": True,
                },
                {
                    "name": "last_transaction",
                    "type": "date",
                    "description": "Date of last transaction",
                    "sensitive": False,
                },
            ],
            "page_context": "Online banking dashboard showing account overview",
        }

    @pytest.fixture
    def mock_dom_simplifier_result(self):
        """Return a SimplifiedDOM-like object."""
        result = MagicMock()
        result.html = "<form><input id='email'><input id='password'><button type='submit'>Sign In</button></form>"
        result.token_estimate = 500
        result.over_budget = False
        return result

    @pytest.mark.asyncio
    async def test_generate_basic(self, login_response, field_response, mock_dom_simplifier_result):
        provider = _make_mock_provider(login_response, field_response)
        page = _make_mock_page()

        generator = BlueprintGenerator(provider)
        generator.dom_simplifier = MagicMock()
        generator.dom_simplifier.simplify = AsyncMock(return_value=mock_dom_simplifier_result)

        result = await generator.generate(
            "https://acmebank.com/login",
            page,
            site_type="banking",
        )

        assert isinstance(result, GeneratedBlueprint)
        assert result.domain == "acmebank.com"
        assert result.site_key == "acmebank_com"
        assert result.confidence == 0.85
        assert result.blueprint.name == "Acme Bank Login"
        assert result.blueprint.extraction_strategy == ExtractionStrategy.LLM_ADAPTIVE
        assert result.blueprint.schema_version == "3.0"
        assert "account_balance" in result.blueprint_json["extract"]
        assert "account_number" in result.blueprint_json["extract"]
        assert result.blueprint_json["extract"]["account_number"].get("sensitive") is True

    @pytest.mark.asyncio
    async def test_generate_with_site_name(self, login_response, field_response, mock_dom_simplifier_result):
        provider = _make_mock_provider(login_response, field_response)
        page = _make_mock_page()

        generator = BlueprintGenerator(provider)
        generator.dom_simplifier = MagicMock()
        generator.dom_simplifier.simplify = AsyncMock(return_value=mock_dom_simplifier_result)

        result = await generator.generate(
            "https://acmebank.com/login",
            page,
            site_name="Acme National Bank",
        )

        assert result.blueprint.name == "Acme National Bank"

    @pytest.mark.asyncio
    async def test_generate_auth_steps(self, login_response, field_response, mock_dom_simplifier_result):
        provider = _make_mock_provider(login_response, field_response)
        page = _make_mock_page()

        generator = BlueprintGenerator(provider)
        generator.dom_simplifier = MagicMock()
        generator.dom_simplifier.simplify = AsyncMock(return_value=mock_dom_simplifier_result)

        result = await generator.generate("https://acmebank.com/login", page)

        auth_steps = result.blueprint_json["auth"]["steps"]
        # Should have: goto, fill username, fill password, click submit, wait
        assert auth_steps[0]["action"] == "goto"
        assert auth_steps[0]["url"] == "https://acmebank.com/login"
        assert auth_steps[1]["action"] == "fill"
        assert auth_steps[1]["selector"] == "#email"
        assert auth_steps[1]["value"] == "{{username}}"
        assert auth_steps[2]["action"] == "fill"
        assert auth_steps[2]["selector"] == "#password"
        assert auth_steps[2]["value"] == "{{password}}"
        assert auth_steps[3]["action"] == "click"
        assert auth_steps[3]["selector"] == "button[type='submit']"
        assert auth_steps[4] == {"action": "wait_for_navigation", "timeout": 10000}

    @pytest.mark.asyncio
    async def test_generate_with_mfa(self, field_response, mock_dom_simplifier_result):
        login_response = {
            "username_selector": "#user",
            "password_selector": "#pass",
            "submit_selector": "#login",
            "pre_login_steps": [],
            "mfa_likely": True,
            "mfa_hints": "OTP input detected",
            "page_title": "Secure Bank",
            "confidence": 0.8,
        }
        provider = _make_mock_provider(login_response, field_response)
        page = _make_mock_page()

        generator = BlueprintGenerator(provider)
        generator.dom_simplifier = MagicMock()
        generator.dom_simplifier.simplify = AsyncMock(return_value=mock_dom_simplifier_result)

        result = await generator.generate("https://securebank.com/login", page)

        assert "mfa" in result.blueprint_json
        assert result.blueprint_json["mfa"]["type"] == "otp_input"
        assert result.blueprint_json["mfa"]["handler"] == "user_prompt"

    @pytest.mark.asyncio
    async def test_generate_with_pre_login_steps(self, field_response, mock_dom_simplifier_result):
        login_response = {
            "username_selector": "#user",
            "password_selector": "#pass",
            "submit_selector": "#login",
            "pre_login_steps": [
                {"action": "click", "selector": "#accept-cookies", "description": "Dismiss cookie banner"},
            ],
            "mfa_likely": False,
            "mfa_hints": "",
            "page_title": "Test Bank",
            "confidence": 0.9,
        }
        provider = _make_mock_provider(login_response, field_response)
        page = _make_mock_page()

        generator = BlueprintGenerator(provider)
        generator.dom_simplifier = MagicMock()
        generator.dom_simplifier.simplify = AsyncMock(return_value=mock_dom_simplifier_result)

        result = await generator.generate("https://testbank.com/login", page)

        auth_steps = result.blueprint_json["auth"]["steps"]
        # goto, click cookie, fill user, fill pass, click submit, wait
        assert auth_steps[1]["action"] == "click"
        assert auth_steps[1]["selector"] == "#accept-cookies"

    @pytest.mark.asyncio
    async def test_generate_low_confidence_triggers_vision(self, field_response, mock_dom_simplifier_result):
        """When DOM analysis confidence < 0.5, vision fallback is attempted."""
        low_conf_login = {
            "username_selector": None,
            "password_selector": None,
            "submit_selector": None,
            "pre_login_steps": [],
            "mfa_likely": False,
            "mfa_hints": "",
            "page_title": "",
            "confidence": 0.2,
        }
        vision_login = {
            "username_selector": "#email-input",
            "password_selector": "#pwd",
            "submit_selector": ".login-btn",
            "pre_login_steps": [],
            "mfa_likely": False,
            "mfa_hints": "",
            "page_title": "Vision Bank",
            "confidence": 0.7,
        }

        provider = MagicMock()
        provider.provider_name = "openai"
        # First call: DOM analysis (low confidence)
        # Second call: field discovery
        provider.extract = AsyncMock(
            side_effect=[
                _make_llm_response(low_conf_login),
                _make_llm_response(field_response),
            ]
        )
        # Vision call uses _call directly
        provider._call = AsyncMock(return_value=_make_llm_response(vision_login))
        provider.close = AsyncMock()

        page = _make_mock_page()

        generator = BlueprintGenerator(provider)
        generator.dom_simplifier = MagicMock()
        generator.dom_simplifier.simplify = AsyncMock(return_value=mock_dom_simplifier_result)

        result = await generator.generate("https://visionbank.com/login", page)

        # Vision result should be used since it has higher confidence
        assert result.confidence == 0.7
        assert result.blueprint_json["auth"]["steps"][1]["selector"] == "#email-input"
        assert "Used vision-based analysis" in result.warnings[0]

    @pytest.mark.asyncio
    async def test_generate_missing_selectors_warning(self, field_response, mock_dom_simplifier_result):
        """Missing username/password selectors should produce warnings."""
        login_response = {
            "username_selector": None,
            "password_selector": "#pass",
            "submit_selector": None,
            "pre_login_steps": [],
            "mfa_likely": False,
            "mfa_hints": "",
            "page_title": "Broken Site",
            "confidence": 0.6,
        }
        provider = _make_mock_provider(login_response, field_response)
        page = _make_mock_page()

        generator = BlueprintGenerator(provider)
        generator.dom_simplifier = MagicMock()
        generator.dom_simplifier.simplify = AsyncMock(return_value=mock_dom_simplifier_result)

        result = await generator.generate("https://brokensite.com/login", page)

        assert any("username" in w.lower() for w in result.warnings)
        assert any("submit button" in w.lower() for w in result.warnings)

    @pytest.mark.asyncio
    async def test_generate_llm_failure_returns_defaults(self, mock_dom_simplifier_result):
        """When both DOM and field discovery fail, defaults are used."""
        provider = MagicMock()
        provider.provider_name = "openai"
        provider.extract = AsyncMock(side_effect=LLMProviderError("API down"))
        provider._call = AsyncMock(side_effect=LLMProviderError("API down"))
        provider.close = AsyncMock()

        page = _make_mock_page()

        generator = BlueprintGenerator(provider)
        generator.dom_simplifier = MagicMock()
        generator.dom_simplifier.simplify = AsyncMock(return_value=mock_dom_simplifier_result)

        result = await generator.generate("https://failsite.com/login", page)

        # Should still produce a blueprint with defaults
        assert result.confidence == 0.0
        assert "account_name" in result.blueprint_json["extract"]
        assert "balance" in result.blueprint_json["extract"]

    @pytest.mark.asyncio
    async def test_generate_extra_fields(self, login_response, field_response, mock_dom_simplifier_result):
        provider = _make_mock_provider(login_response, field_response)
        page = _make_mock_page()

        generator = BlueprintGenerator(provider)
        generator.dom_simplifier = MagicMock()
        generator.dom_simplifier.simplify = AsyncMock(return_value=mock_dom_simplifier_result)

        result = await generator.generate(
            "https://acmebank.com/login",
            page,
            extra_fields=[
                {"name": "routing_number", "type": "text", "description": "Bank routing number"},
            ],
        )

        assert "routing_number" in result.blueprint_json["extract"]

    @pytest.mark.asyncio
    async def test_generated_blueprint_validates(self, login_response, field_response, mock_dom_simplifier_result):
        """The generated blueprint should pass BlueprintV2 validation."""
        provider = _make_mock_provider(login_response, field_response)
        page = _make_mock_page()

        generator = BlueprintGenerator(provider)
        generator.dom_simplifier = MagicMock()
        generator.dom_simplifier.simplify = AsyncMock(return_value=mock_dom_simplifier_result)

        result = await generator.generate("https://acmebank.com/login", page)

        # Should be a valid BlueprintV2
        assert isinstance(result.blueprint, BlueprintV2)
        assert result.blueprint.is_llm_adaptive
        assert result.blueprint.domain == "acmebank.com"

    @pytest.mark.asyncio
    async def test_generate_tags(self, login_response, field_response, mock_dom_simplifier_result):
        provider = _make_mock_provider(login_response, field_response)
        page = _make_mock_page()

        generator = BlueprintGenerator(provider)
        generator.dom_simplifier = MagicMock()
        generator.dom_simplifier.simplify = AsyncMock(return_value=mock_dom_simplifier_result)

        result = await generator.generate(
            "https://acmebank.com/login",
            page,
            site_type="banking",
        )

        assert "auto_generated" in result.blueprint_json["tags"]
        assert "banking" in result.blueprint_json["tags"]

    @pytest.mark.asyncio
    async def test_generate_rate_limit_present(self, login_response, field_response, mock_dom_simplifier_result):
        provider = _make_mock_provider(login_response, field_response)
        page = _make_mock_page()

        generator = BlueprintGenerator(provider)
        generator.dom_simplifier = MagicMock()
        generator.dom_simplifier.simplify = AsyncMock(return_value=mock_dom_simplifier_result)

        result = await generator.generate("https://acmebank.com/login", page)

        assert "rate_limit" in result.blueprint_json
        assert result.blueprint_json["rate_limit"]["max_requests_per_hour"] == 10


# ── Generator safety (SEC-03) ─────────────────────────────────────────────────


class TestGeneratorSafety:
    @pytest.mark.asyncio
    async def test_credentials_in_the_url_are_refused(self):
        generator = BlueprintGenerator(MagicMock())
        with pytest.raises(ValueError, match="credentials"):
            await generator.generate("https://chase:x@attacker.example/login", _make_mock_page())

    @pytest.mark.asyncio
    async def test_site_key_and_domain_come_from_the_hostname(self):
        provider = _make_mock_provider(
            {"username_selector": "#u", "password_selector": "#p", "submit_selector": "#go", "confidence": 0.9},
            {"fields": [{"name": "balance", "type": "currency"}]},
        )
        generator = BlueprintGenerator(provider)
        generator.dom_simplifier = MagicMock()
        generator.dom_simplifier.simplify = AsyncMock(return_value=MagicMock(html="<form></form>"))
        page = _make_mock_page()
        page.eval_on_selector = AsyncMock(return_value="https://login.attacker.example/session?next=/")

        result = await generator.generate("https://Attacker.Example:443/login", page)

        assert result.site_key == "attacker_example"
        assert result.blueprint_json["domain"] == "attacker.example"
        assert "auto_generated" in result.blueprint_json["tags"]
        # Where the login form posts is read from the page, not invented.
        assert result.blueprint_json["auth"]["submit_targets"] == ["https://login.attacker.example/session"]

    @pytest.mark.asyncio
    async def test_model_suggested_steps_and_fields_are_sanitised(self):
        provider = _make_mock_provider(
            {
                "username_selector": "#u",
                "password_selector": "#p",
                "submit_selector": "#go",
                "pre_login_steps": [
                    {"action": "goto", "selector": "file:///etc/passwd"},
                    {"action": "execute_js", "selector": "#x"},
                    {"action": "click", "selector": "#cookies"},
                ],
                "confidence": 0.9,
            },
            {"fields": [{"name": "Balance Due!", "type": "money"}, "junk", {"name": ""}]},
        )
        generator = BlueprintGenerator(provider)
        generator.dom_simplifier = MagicMock()
        generator.dom_simplifier.simplify = AsyncMock(return_value=MagicMock(html="<form></form>"))

        result = await generator.generate("https://bank.example/login", _make_mock_page())

        actions = [step["action"] for step in result.blueprint_json["auth"]["steps"]]
        assert actions == ["goto", "click", "fill", "fill", "click", "wait_for_navigation"]
        assert result.blueprint_json["extract"] == {"balance_due": {"type": "text", "description": ""}}
        assert isinstance(result.blueprint, BlueprintV2)


# ── API Endpoint Tests ────────────────────────────────────────────────────────


def _admin_headers(client):
    from src.database import User, get_db

    response = client.post(
        "/auth/register",
        json={"username": "gen_admin", "email": "gen_admin@example.com", "password": "Secure@pass123"},
    )
    assert response.status_code == 200
    gen = client.app.dependency_overrides[get_db]()
    db = next(gen)
    try:
        user = db.query(User).filter(User.username == "gen_admin").first()
        user.is_admin = True
        db.commit()
    finally:
        gen.close()
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


class _FakePage:
    async def goto(self, *args, **kwargs):
        return None

    async def wait_for_timeout(self, ms):
        return None


class _FakeContext:
    async def new_page(self):
        return _FakePage()


class _FakeLease:
    network_violation = None
    context = _FakeContext()


class _FakePool:
    def __init__(self):
        self.acquired = []
        self.released = []

    async def acquire(self, session_id, **kwargs):
        self.acquired.append(kwargs)
        return _FakeLease()

    async def release(self, session_id):
        self.released.append(session_id)


def _draft():
    from types import SimpleNamespace

    blueprint_json = {
        "schema_version": "3.0",
        "name": "Bank",
        "domain": "bank.example",
        "tags": ["auto_generated"],
        "extraction_strategy": "llm_adaptive",
        "auth": {
            "type": "form",
            "submit_targets": ["/*"],
            "steps": [{"action": "goto", "url": "https://bank.example/login"}],
        },
        "extract": {"balance": {"type": "currency"}},
    }
    return SimpleNamespace(
        blueprint_json=blueprint_json,
        site_key="bank_example",
        domain="bank.example",
        confidence=0.9,
        warnings=[],
    )


class TestBlueprintGenerateEndpoint:
    """Test the POST /blueprints/generate endpoint."""

    def test_requires_auth(self, client):
        response = client.post("/blueprints/generate", json={"url": "https://example.com"})
        assert response.status_code == 401

    def test_requires_an_administrator(self, client, auth_headers):
        response = client.post("/blueprints/generate", json={"url": "https://example.com"}, headers=auth_headers)
        assert response.status_code == 403

    def test_requires_url(self, client):
        response = client.post("/blueprints/generate", json={}, headers=_admin_headers(client))
        assert response.status_code == 422

    def test_rejects_invalid_scheme(self, client):
        response = client.post(
            "/blueprints/generate",
            json={"url": "ftp://example.com"},
            headers=_admin_headers(client),
        )
        assert response.status_code == 422
        assert "http" in response.json()["detail"].lower()

    def test_rejects_missing_hostname(self, client):
        response = client.post(
            "/blueprints/generate",
            json={"url": "https://"},
            headers=_admin_headers(client),
        )
        assert response.status_code == 422

    def test_rejects_credentials_in_the_url(self, client):
        response = client.post(
            "/blueprints/generate",
            json={"url": "https://chase:x@attacker.example/login"},
            headers=_admin_headers(client),
        )
        assert response.status_code == 422
        assert "credentials" in response.json()["detail"]

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:8000/",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.5/",
            "http://[::1]/",
            "http://168.63.129.16/",
        ],
    )
    def test_rejects_private_addresses(self, client, url):
        with patch("src.routers.system.settings.llm_api_key", "sk-test"):
            response = client.post("/blueprints/generate", json={"url": url}, headers=_admin_headers(client))
        assert response.status_code == 422

    def test_rejects_unresolvable_hostnames(self, client):
        with patch("src.routers.system._resolves", AsyncMock(return_value=False)):
            response = client.post(
                "/blueprints/generate",
                json={"url": "https://no-such-host.invalid/login"},
                headers=_admin_headers(client),
            )
        assert response.status_code == 422
        assert "resolve" in response.json()["detail"]

    def test_requires_llm_configured(self, client):
        """Should return 503 if LLM_API_KEY is not set."""
        headers = _admin_headers(client)
        with (
            patch("src.routers.system.settings") as mock_settings,
            patch("src.routers.system._resolves", AsyncMock(return_value=True)),
        ):
            mock_settings.llm_api_key = ""
            mock_settings.llm_provider = "openai"
            mock_settings.llm_model = "gpt-4o-mini"
            mock_settings.demo_mode = False
            mock_settings.engine_allow_internal_connectors = False
            response = client.post(
                "/blueprints/generate",
                json={"url": "https://example.com/login"},
                headers=headers,
            )
            assert response.status_code == 503

    def _generate(self, client, tmp_path, headers, *, save):
        pool = _FakePool()
        with (
            patch("src.routers.system.settings.llm_api_key", "sk-test"),
            patch("src.routers.system.settings.connectors_dir", str(tmp_path)),
            patch("src.routers.system.get_browser_pool", AsyncMock(return_value=pool)),
            patch("src.routers.system._resolves", AsyncMock(return_value=True)),
            patch("src.core.blueprint_generator.BlueprintGenerator") as Generator,
        ):
            Generator.return_value.generate = AsyncMock(return_value=_draft())
            response = client.post(
                "/blueprints/generate",
                json={"url": "https://bank.example/login", "save": save},
                headers=headers,
            )
        return response, pool

    def test_returns_a_draft_without_writing_anything(self, client, tmp_path):
        response, pool = self._generate(client, tmp_path, _admin_headers(client), save=False)
        assert response.status_code == 200, response.text
        assert response.json()["saved"] is None
        assert response.json()["site_key"] == "bank_example"
        assert list(tmp_path.iterdir()) == []
        # The browser session is held to the address policy and never submits forms.
        [lease] = pool.acquired
        assert lease["address_policy"].block_private is True
        assert lease["read_only_policy"].phase.value == "read"
        assert len(pool.released) == 1

    def test_saving_writes_once_under_the_hostname_key(self, client, tmp_path):
        headers = _admin_headers(client)
        response, _ = self._generate(client, tmp_path, headers, save=True)
        assert response.status_code == 200, response.text
        saved = tmp_path / "bank_example.json"
        assert response.json()["saved"] == str(saved)
        assert json.loads(saved.read_text())["domain"] == "bank.example"

        again, _ = self._generate(client, tmp_path, headers, save=True)
        assert again.status_code == 409
