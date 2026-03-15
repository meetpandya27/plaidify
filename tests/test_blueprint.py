"""
Tests for Blueprint V2 schema — parsing, validation, and V1 conversion.
"""

import json
import pytest
from pathlib import Path
from pydantic import ValidationError

from src.core.blueprint import (
    AuthConfig,
    AuthType,
    BlueprintStep,
    BlueprintV2,
    ExtractionField,
    FieldType,
    ListExtractionField,
    MFAConfig,
    MFADetection,
    MFAType,
    StepAction,
    TransformType,
    convert_v1_to_v2,
    load_blueprint,
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
        field = ExtractionField(
            selector="#ssn", type=FieldType.TEXT, sensitive=True
        )
        assert field.sensitive is True

    def test_field_with_default(self):
        field = ExtractionField(
            selector="#missing", type=FieldType.TEXT, default="N/A"
        )
        assert field.default == "N/A"

    def test_field_with_attribute(self):
        field = ExtractionField(
            selector="a.link", type=FieldType.TEXT, attribute="href"
        )
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
                schema_version="3.0",
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
            "login_url": "https://demo.example.com/login",
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
        assert bp.domain == "demo.example.com"
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
        bp_file.write_text(json.dumps({
            "name": "File Test",
            "login_url": "https://file.com/login",
            "fields": {"username": "#u", "password": "#p", "submit": "#s"},
            "post_login": [{"extract": {"data": "#d"}}],
        }))
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

    def test_load_existing_demo_blueprint(self):
        """Test that the existing demo_site.json loads (V1 auto-convert)."""
        path = Path("connectors/demo_site.json")
        if path.exists():
            bp = load_blueprint(path)
            assert bp.name == "Demo Site"
            assert bp.schema_version == "2.0"

    def test_load_test_bank_blueprint(self):
        """Test that the new test_bank.json V2 blueprint loads."""
        path = Path("connectors/test_bank.json")
        if path.exists():
            bp = load_blueprint(path)
            assert bp.name == "Test Bank"
            assert bp.schema_version == "2.0"
            assert bp.mfa is not None
            assert "balance" in bp.extract
            assert "transactions" in bp.extract

    def test_invalid_json_raises(self, tmp_path):
        bp_file = tmp_path / "bad.json"
        bp_file.write_text("not valid json {{{")
        with pytest.raises(json.JSONDecodeError):
            load_blueprint(bp_file)
