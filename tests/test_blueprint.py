"""
Tests for Blueprint V2 schema — parsing, validation, and V1 conversion.
"""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.core.blueprint import (
    AuthConfig,
    AuthType,
    BlueprintStep,
    BlueprintV2,
    ExtractionField,
    ExtractionStrategy,
    FieldType,
    ListExtractionField,
    MFAConfig,
    MFADetection,
    MFAType,
    StepAction,
    TransformType,
    convert_v1_to_v2,
    load_blueprint,
    load_blueprint_from_dict,
)

# ── Step Model Tests ──────────────────────────────────────────────────────────


class TestBlueprintStep:
    def test_goto_step(self):
        step = BlueprintStep(action=StepAction.GOTO, url="https://example.com/login")
        assert step.action == StepAction.GOTO
        assert step.url == "https://example.com/login"

    def test_fill_step(self):
        step = BlueprintStep(action=StepAction.FILL, selector="#username", value="{{username}}")
        assert step.action == StepAction.FILL
        assert step.selector == "#username"
        assert step.value == "{{username}}"

    def test_click_step_with_navigation(self):
        step = BlueprintStep(action=StepAction.CLICK, selector="#submit", wait_for_navigation=True)
        assert step.wait_for_navigation is True

    def test_wait_step_with_timeout(self):
        step = BlueprintStep(action=StepAction.WAIT, selector="#dashboard", timeout=5000)
        assert step.timeout == 5000

    def test_conditional_step(self):
        step = BlueprintStep(
            action=StepAction.CONDITIONAL,
            condition_selector="#error-msg",
            then_steps=[
                BlueprintStep(action=StepAction.SCREENSHOT, screenshot_name="error"),
            ],
        )
        assert step.condition_selector == "#error-msg"
        assert len(step.then_steps) == 1


# ── Extraction Field Tests ────────────────────────────────────────────────────


class TestExtractionField:
    def test_basic_text_field(self):
        field = ExtractionField(selector="#name", type=FieldType.TEXT)
        assert field.type == FieldType.TEXT
        assert field.sensitive is False

    def test_currency_field_with_transform(self):
        field = ExtractionField(
            selector="#balance",
            type=FieldType.CURRENCY,
            transform=TransformType.STRIP_DOLLAR_SIGN,
        )
        assert field.type == FieldType.CURRENCY
        assert field.transform == TransformType.STRIP_DOLLAR_SIGN

    def test_sensitive_field(self):
        field = ExtractionField(selector="#ssn", type=FieldType.TEXT, sensitive=True)
        assert field.sensitive is True

    def test_field_with_default(self):
        field = ExtractionField(selector="#missing", type=FieldType.TEXT, default="N/A")
        assert field.default == "N/A"

    def test_field_with_attribute(self):
        field = ExtractionField(selector="a.link", type=FieldType.TEXT, attribute="href")
        assert field.attribute == "href"


# ── List Extraction Tests ─────────────────────────────────────────────────────


class TestListExtractionField:
    def test_basic_list(self):
        field = ListExtractionField(
            selector=".row",
            type=FieldType.LIST,
            fields={
                "name": ExtractionField(selector=".name", type=FieldType.TEXT),
                "amount": ExtractionField(selector=".amount", type=FieldType.CURRENCY),
            },
        )
        assert len(field.fields) == 2
        assert field.max_items is None

    def test_list_with_max_items(self):
        field = ListExtractionField(
            selector=".row",
            type=FieldType.LIST,
            fields={"col": ExtractionField(selector=".col", type=FieldType.TEXT)},
            max_items=10,
        )
        assert field.max_items == 10


# ── MFA Config Tests ─────────────────────────────────────────────────────────


class TestMFAConfig:
    def test_otp_config(self):
        mfa = MFAConfig(
            detection=MFADetection(selector="#otp-input", timeout=3000),
            type=MFAType.OTP_INPUT,
            input_selector="#otp-input",
            submit_selector="#otp-submit",
        )
        assert mfa.type == MFAType.OTP_INPUT
        assert mfa.detection.timeout == 3000

    def test_push_config(self):
        mfa = MFAConfig(
            detection=MFADetection(selector="#push-notice"),
            type=MFAType.PUSH,
            poll_interval=3000,
            poll_timeout=120000,
        )
        assert mfa.poll_interval == 3000


# ── Full Blueprint V2 Tests ──────────────────────────────────────────────────


class TestBlueprintV2:
    def test_minimal_blueprint(self):
        bp = BlueprintV2(
            schema_version="2.0",
            name="Test Site",
            domain="test.com",
            auth=AuthConfig(
                type=AuthType.FORM,
                steps=[
                    BlueprintStep(action=StepAction.GOTO, url="https://test.com/login"),
                    BlueprintStep(action=StepAction.FILL, selector="#user", value="{{username}}"),
                    BlueprintStep(action=StepAction.CLICK, selector="#submit"),
                ],
            ),
        )
        assert bp.schema_version == "2.0"
        assert bp.name == "Test Site"
        assert len(bp.auth.steps) == 3

    def test_full_blueprint(self):
        bp = BlueprintV2(
            schema_version="2.0",
            name="Full Site",
            domain="full.example.com",
            tags=["banking", "us"],
            auth=AuthConfig(
                type=AuthType.FORM,
                steps=[BlueprintStep(action=StepAction.GOTO, url="https://full.example.com")],
            ),
            mfa=MFAConfig(
                detection=MFADetection(selector="#mfa"),
                type=MFAType.OTP_INPUT,
                input_selector="#mfa input",
            ),
            extract={
                "balance": ExtractionField(selector="#bal", type=FieldType.CURRENCY),
            },
        )
        assert bp.mfa is not None
        assert "balance" in bp.extract

    def test_invalid_schema_version(self):
        with pytest.raises(ValidationError):
            BlueprintV2(
                schema_version="4.0",
                name="Bad",
                domain="bad.com",
                auth=AuthConfig(
                    type=AuthType.FORM,
                    steps=[BlueprintStep(action=StepAction.GOTO, url="https://bad.com")],
                ),
            )


# ── V1 Conversion Tests ──────────────────────────────────────────────────────


class TestV1Conversion:
    def test_convert_basic_v1(self):
        v1 = {
            "name": "Demo Site",
            "login_url": "https://fixture.example.com/login",
            "fields": {
                "username": "#user",
                "password": "#pass",
                "submit": "#login-btn",
            },
            "post_login": [
                {"wait": "#dashboard"},
                {"extract": {"status": "#status", "synced": "#sync"}},
            ],
        }
        bp = convert_v1_to_v2(v1)
        assert bp.schema_version == "2.0"
        assert bp.name == "Demo Site"
        assert bp.domain == "fixture.example.com"
        # goto + fill(user) + fill(pass) + click(submit) + wait(dashboard)
        assert len(bp.auth.steps) == 5
        assert "status" in bp.extract
        assert "synced" in bp.extract

    def test_convert_minimal_v1(self):
        v1 = {
            "name": "Minimal",
            "login_url": "https://minimal.com/login",
            "fields": {},
            "post_login": [],
        }
        bp = convert_v1_to_v2(v1)
        assert bp.name == "Minimal"
        assert len(bp.auth.steps) == 1  # Just goto


# ── Load Blueprint from File ─────────────────────────────────────────────────


class TestLoadBlueprint:
    def test_load_v1_blueprint(self, tmp_path):
        bp_file = tmp_path / "site.json"
        bp_file.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "name": "File Test",
                    "login_url": "https://file.com/login",
                    "fields": {"username": "#u", "password": "#p", "submit": "#s"},
                    "post_login": [{"extract": {"data": "#d"}}],
                }
            )
        )
        bp = load_blueprint(bp_file)
        assert bp.name == "File Test"
        assert bp.schema_version == "2.0"

    def test_load_v2_blueprint(self, tmp_path):
        bp_file = tmp_path / "site.json"
        bp_data = {
            "schema_version": "2.0",
            "name": "V2 Test",
            "domain": "v2.com",
            "auth": {
                "type": "form",
                "steps": [{"action": "goto", "url": "https://v2.com"}],
            },
            "extract": {
                "name": {"selector": "#name", "type": "text"},
            },
        }
        bp_file.write_text(json.dumps(bp_data))
        bp = load_blueprint(bp_file)
        assert bp.name == "V2 Test"
        assert bp.schema_version == "2.0"

    def test_load_internal_fixture_blueprint(self):
        """Test that the existing internal_bank.json loads."""
        path = Path("connectors/internal_bank.json")
        if path.exists():
            bp = load_blueprint(path)
            assert bp.name == "Internal Browser Fixture"
            assert bp.schema_version == "2.0"

    def test_load_internal_bank_blueprint(self):
        """Test that the internal_bank.json V2 blueprint loads."""
        path = Path("connectors/internal_bank.json")
        if path.exists():
            bp = load_blueprint(path)
            assert bp.name == "Internal Browser Fixture"
            assert bp.schema_version == "2.0"
            assert bp.mfa is not None
            assert "current_bill" in bp.extract
            assert "usage_history" in bp.extract

    def test_load_hydro_one_blueprint(self):
        """Test that the hydro_one.json V2 blueprint loads."""
        path = Path("connectors/hydro_one.json")
        if path.exists():
            bp = load_blueprint(path)
            assert bp.name == "Hydro One"
            assert bp.schema_version == "2.0"
            assert bp.mfa is not None
            assert "current_balance" in bp.extract
            assert "usage_kwh" in bp.extract

    def test_invalid_json_raises(self, tmp_path):
        bp_file = tmp_path / "bad.json"
        bp_file.write_text("not valid json {{{")
        with pytest.raises(json.JSONDecodeError):
            load_blueprint(bp_file)


# ── Blueprint V3 Tests ───────────────────────────────────────────────────────


class TestBlueprintV3:
    """Tests for V3 schema features (LLM-adaptive extraction)."""

    def _make_auth(self):
        return AuthConfig(
            type=AuthType.FORM,
            steps=[BlueprintStep(action=StepAction.GOTO, url="https://example.com")],
        )

    def test_v3_schema_version(self):
        bp = BlueprintV2(
            schema_version="3.0",
            name="V3 Site",
            domain="v3.com",
            auth=self._make_auth(),
            extraction_strategy=ExtractionStrategy.LLM_ADAPTIVE,
        )
        assert bp.schema_version == "3.0"
        assert bp.is_llm_adaptive

    def test_v2_is_not_llm_adaptive(self):
        bp = BlueprintV2(
            schema_version="2.0",
            name="V2 Site",
            domain="v2.com",
            auth=self._make_auth(),
        )
        assert not bp.is_llm_adaptive
        assert bp.extraction_strategy == ExtractionStrategy.SELECTOR

    def test_v3_fields_without_selectors(self):
        """V3 fields can omit selectors and use descriptions instead."""
        bp = BlueprintV2(
            schema_version="3.0",
            name="LLM Site",
            domain="llm.com",
            auth=self._make_auth(),
            extraction_strategy=ExtractionStrategy.LLM_ADAPTIVE,
            page_context="Utility bill dashboard",
            extract={
                "account_number": ExtractionField(
                    type=FieldType.TEXT,
                    description="The customer's account number",
                    sensitive=True,
                ),
                "balance": ExtractionField(
                    type=FieldType.CURRENCY,
                    description="Amount currently owed",
                    example="$1,234.56",
                ),
            },
        )
        assert bp.extract["account_number"].selector is None
        assert bp.extract["account_number"].description == "The customer's account number"
        assert bp.extract["balance"].example == "$1,234.56"
        assert bp.page_context == "Utility bill dashboard"

    def test_v3_with_fallback_selectors(self):
        bp = BlueprintV2(
            schema_version="3.0",
            name="Fallback Site",
            domain="fallback.com",
            auth=self._make_auth(),
            extraction_strategy=ExtractionStrategy.LLM_ADAPTIVE,
            extract={
                "account_number": ExtractionField(
                    type=FieldType.TEXT,
                    description="Account ID",
                    fallback_selector="span.acc-num",
                ),
            },
            fallback_selectors={"account_number": "span.acc-num"},
        )
        assert bp.fallback_selectors == {"account_number": "span.acc-num"}
        assert bp.extract["account_number"].fallback_selector == "span.acc-num"

    def test_v3_list_field_without_selector(self):
        bp = BlueprintV2(
            schema_version="3.0",
            name="List Site",
            domain="list.com",
            auth=self._make_auth(),
            extraction_strategy=ExtractionStrategy.LLM_ADAPTIVE,
            extract={
                "usage_history": ListExtractionField(
                    type=FieldType.LIST,
                    description="Monthly usage records",
                    fields={
                        "month": ExtractionField(type=FieldType.TEXT, description="Billing month"),
                        "kwh": ExtractionField(type=FieldType.NUMBER, description="kWh consumed"),
                        "cost": ExtractionField(type=FieldType.CURRENCY, description="Dollar amount"),
                    },
                ),
            },
        )
        list_field = bp.extract["usage_history"]
        assert list_field.selector is None
        assert list_field.description == "Monthly usage records"

    def test_v3_backward_compat_v2_still_works(self):
        """V2 blueprints with selectors should still work exactly as before."""
        bp = BlueprintV2(
            schema_version="2.0",
            name="Old Style",
            domain="old.com",
            auth=self._make_auth(),
            extract={
                "balance": ExtractionField(
                    selector="span.balance",
                    type=FieldType.CURRENCY,
                ),
            },
        )
        assert bp.extract["balance"].selector == "span.balance"
        assert not bp.is_llm_adaptive

    def test_load_v3_from_file(self, tmp_path):
        bp_data = {
            "schema_version": "3.0",
            "name": "V3 File Test",
            "domain": "v3file.com",
            "extraction_strategy": "llm_adaptive",
            "page_context": "Energy dashboard",
            "auth": {
                "type": "form",
                "steps": [{"action": "goto", "url": "https://v3file.com"}],
            },
            "extract": {
                "account_number": {
                    "type": "text",
                    "description": "Account ID",
                    "sensitive": True,
                },
                "balance": {
                    "type": "currency",
                    "description": "Current balance",
                    "example": "$100.00",
                },
            },
            "fallback_selectors": {
                "account_number": "span.acc-num",
            },
        }
        bp_file = tmp_path / "v3.json"
        bp_file.write_text(json.dumps(bp_data))
        bp = load_blueprint(bp_file)
        assert bp.schema_version == "3.0"
        assert bp.is_llm_adaptive
        assert bp.page_context == "Energy dashboard"
        assert bp.fallback_selectors == {"account_number": "span.acc-num"}


# ── Strict schema (ENG-14) ───────────────────────────────────────────────────


def _v2(**overrides):
    data = {
        "schema_version": "2.0",
        "name": "Strict",
        "domain": "strict.example",
        "auth": {
            "type": "form",
            "steps": [
                {"action": "goto", "url": "https://strict.example/login"},
                {"action": "fill", "selector": "#u", "value": "{{username}}"},
                {"action": "click", "selector": "#go", "wait_for_navigation": True},
            ],
        },
        "extract": {"balance": {"selector": "#balance", "type": "currency"}},
    }
    data.update(overrides)
    return data


def _with_steps(*steps):
    return _v2(auth={"type": "form", "steps": list(steps)})


class TestStrictSchema:
    def test_valid_blueprint_loads(self):
        assert load_blueprint_from_dict(_v2()).name == "Strict"

    def test_missing_schema_version_is_an_error_not_v1(self):
        data = _v2()
        del data["schema_version"]
        with pytest.raises(ValueError, match="schema_version is required"):
            load_blueprint_from_dict(data)

    def test_schema_version_required_on_the_model(self):
        with pytest.raises(ValidationError):
            BlueprintV2(
                name="x",
                domain="x.example",
                auth=AuthConfig(steps=[BlueprintStep(action=StepAction.GOTO, url="https://x.example")]),
            )

    @pytest.mark.parametrize(
        "location, data",
        [
            ("top level", _v2(descriptoin="typo")),
            ("step", _with_steps({"action": "click", "selector": "#a", "wait_for_navigaton": True})),
            ("step timeout typo", _with_steps({"action": "wait", "selector": "#a", "timout": 5})),
            ("field", _v2(extract={"balance": {"selector": "#b", "tpye": "currency"}})),
            ("rate limit", _v2(rate_limit={"max_requests_per_minute": 3})),
        ],
    )
    def test_unknown_keys_are_rejected(self, location, data):
        with pytest.raises(ValidationError):
            load_blueprint_from_dict(data)

    @pytest.mark.parametrize(
        "step, message",
        [
            ({"action": "goto"}, "require url"),
            ({"action": "fill", "value": "x"}, "require selector"),
            ({"action": "fill", "selector": "#a"}, "require value"),
            ({"action": "click"}, "require selector"),
            ({"action": "select", "selector": "#a"}, "require value"),
            ({"action": "wait"}, "require a selector, or a timeout"),
            ({"action": "execute_js"}, "require script"),
            ({"action": "conditional", "condition_selector": "#a"}, "then_steps and/or else_steps"),
            ({"action": "iframe", "iframe_selector": "#f"}, "require steps"),
            ({"action": "iframe", "steps": [{"action": "click", "selector": "#a"}]}, "iframe_selector"),
        ],
    )
    def test_each_action_requires_its_fields(self, step, message):
        with pytest.raises(ValidationError, match=message):
            load_blueprint_from_dict(_with_steps({"action": "goto", "url": "https://strict.example"}, step))

    def test_fields_that_do_not_apply_to_an_action_are_rejected(self):
        with pytest.raises(ValidationError, match="do not take url"):
            load_blueprint_from_dict(_with_steps({"action": "click", "selector": "#a", "url": "https://x"}))

    @pytest.mark.parametrize("timeout", [0, -5, 10**9])
    def test_out_of_range_timeouts_are_rejected(self, timeout):
        with pytest.raises(ValidationError):
            load_blueprint_from_dict(_with_steps({"action": "wait", "selector": "#a", "timeout": timeout}))

    def test_extract_action_no_longer_exists(self):
        with pytest.raises(ValidationError):
            load_blueprint_from_dict(_with_steps({"action": "extract", "selector": "#a"}))

    def test_unknown_placeholder_is_rejected(self):
        with pytest.raises(ValidationError, match="unknown variable"):
            load_blueprint_from_dict(_with_steps({"action": "fill", "selector": "#p", "value": "{{pasword}}"}))

    @pytest.mark.parametrize(
        "url", ["file:///etc/passwd", "javascript:alert(1)", "chrome://settings", "data:text/html,x"]
    )
    def test_goto_only_accepts_http_urls(self, url):
        with pytest.raises(ValidationError, match="http"):
            load_blueprint_from_dict(_with_steps({"action": "goto", "url": url}))

    def test_wait_with_only_a_timeout_is_a_pause(self):
        bp = load_blueprint_from_dict(_with_steps({"action": "wait", "timeout": 500}))
        assert bp.auth.steps[0].selector is None and bp.auth.steps[0].timeout == 500

    def test_iframe_step_with_nested_steps(self):
        bp = load_blueprint_from_dict(
            _with_steps(
                {"action": "iframe", "iframe_selector": "#login", "steps": [{"action": "click", "selector": "#a"}]}
            )
        )
        assert bp.auth.steps[0].steps[0].selector == "#a"

    def test_selector_strategy_needs_selectors(self):
        with pytest.raises(ValidationError, match="needs a selector"):
            load_blueprint_from_dict(_v2(extract={"balance": {"type": "currency", "description": "x"}}))

    def test_code_mfa_needs_an_input_selector(self):
        with pytest.raises(ValidationError, match="input_selector"):
            load_blueprint_from_dict(_v2(mfa={"detection": {"selector": "#otp"}, "type": "otp_input"}))

    def test_push_mfa_does_not_need_an_input_selector(self):
        bp = load_blueprint_from_dict(_v2(mfa={"detection": {"selector": "#push"}, "type": "push"}))
        assert bp.mfa.type == MFAType.PUSH

    def test_declared_targets_must_stay_on_the_blueprints_domains(self):
        with pytest.raises(ValidationError, match="not on the blueprint's domain"):
            load_blueprint_from_dict(_v2(logout_targets=["https://evil.example/logout"]))
        bp = load_blueprint_from_dict(_v2(logout_targets=["https://login.strict.example/logout", "/signout"]))
        assert len(bp.cleanup_targets()) == 2

    def test_domain_must_be_a_bare_host(self):
        for bad in ("https://strict.example", "strict.example/path", "user@strict.example"):
            with pytest.raises(ValidationError):
                load_blueprint_from_dict(_v2(domain=bad))

    def test_outcome_checks_need_a_selector_or_valid_text(self):
        with pytest.raises(ValidationError, match="selector and/or a text"):
            load_blueprint_from_dict(_v2(auth={**_v2()["auth"], "failure": {}}))
        with pytest.raises(ValidationError, match="regular expression"):
            load_blueprint_from_dict(_v2(auth={**_v2()["auth"], "failure": {"text": "(unclosed"}}))

    def test_fallback_selectors_must_name_known_fields(self):
        with pytest.raises(ValidationError, match="unknown fields"):
            load_blueprint_from_dict(_v2(fallback_selectors={"nope": "#x"}))

    def test_sensitive_field_names(self):
        bp = load_blueprint_from_dict(
            _v2(
                extract={
                    "account": {"selector": "#a", "sensitive": True},
                    "balance": {"selector": "#b"},
                    "rows": {
                        "selector": ".r",
                        "type": "list",
                        "fields": {"card": {"selector": ".c", "sensitive": True}, "amt": {"selector": ".a"}},
                    },
                }
            )
        )
        assert bp.sensitive_field_names() == ["account", "rows[].card"]


class TestBundledConnectors:
    def test_every_bundled_connector_loads(self):
        from src.core.blueprint import BUNDLED_CONNECTORS_DIR

        files = sorted(BUNDLED_CONNECTORS_DIR.glob("*.json"))
        assert files
        for path in files:
            load_blueprint(path)

    def test_every_json_in_connectors_is_listed_as_bundled(self):
        from src.core.blueprint import BUNDLED_CONNECTOR_SITES, BUNDLED_CONNECTORS_DIR

        assert {p.stem for p in BUNDLED_CONNECTORS_DIR.glob("*.json")} == set(BUNDLED_CONNECTOR_SITES)

    def test_hydro_one_rate_limit_is_read(self):
        bp = load_blueprint(Path("connectors/hydro_one.json"))
        assert bp.rate_limit.max_requests_per_hour == 180
        assert bp.rate_limit.min_interval_seconds == 30
        assert bp.auth.failure is not None and bp.auth.success is not None

    def test_bundled_connectors_declare_targets_and_outcomes(self):
        for name in ("demo_bank", "demo_saas", "demo_utility", "internal_bank", "hydro_one"):
            bp = load_blueprint(Path(f"connectors/{name}.json"))
            assert bp.auth.submit_targets, name
            assert bp.auth.failure is not None, name
            assert bp.logout_targets, name
            if bp.mfa is not None:
                assert bp.mfa.submit_targets, name


class TestTrustAndExecution:
    def test_bundled_connector_is_trusted(self):
        from src.core.blueprint import BUNDLED_CONNECTORS_DIR, TrustTier, resolve_trust_tier

        path = BUNDLED_CONNECTORS_DIR / "hydro_one.json"
        assert resolve_trust_tier(path, load_blueprint(path)) is TrustTier.BUNDLED

    def test_copy_outside_the_repo_is_untrusted(self, tmp_path):
        from src.core.blueprint import TrustTier, resolve_trust_tier

        copy = tmp_path / "hydro_one.json"
        copy.write_text(Path("connectors/hydro_one.json").read_text())
        tier = resolve_trust_tier(copy, load_blueprint(copy))
        assert tier is TrustTier.UNTRUSTED
        assert not tier.allows_javascript

    def test_operator_can_vouch_for_a_connector(self, tmp_path):
        from src.core.blueprint import TrustTier, resolve_trust_tier

        path = tmp_path / "intranet.json"
        path.write_text(json.dumps(_v2()))
        bp = load_blueprint(path)
        assert resolve_trust_tier(path, bp, operator_trusted=["intranet"]) is TrustTier.OPERATOR
        assert resolve_trust_tier(path, bp, operator_trusted=["other"]) is TrustTier.UNTRUSTED

    def test_generated_blueprints_are_never_trusted(self, tmp_path):
        from src.core.blueprint import TrustTier, resolve_trust_tier

        path = tmp_path / "gen.json"
        path.write_text(json.dumps(_v2(tags=["auto_generated"])))
        assert resolve_trust_tier(path, load_blueprint(path), operator_trusted=["gen"]) is TrustTier.UNTRUSTED

    @pytest.mark.parametrize(
        "tags, demo, allow, expected",
        [
            (["utility"], False, False, True),
            (["internal", "fixture"], False, False, False),
            (["sandbox", "demo"], False, False, False),
            (["internal"], True, False, True),
            (["sandbox"], False, True, True),
        ],
    )
    def test_executability(self, tags, demo, allow, expected):
        from src.core.blueprint import blueprint_is_executable

        assert blueprint_is_executable(tags, demo_mode=demo, allow_internal=allow) is expected
