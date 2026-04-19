"""Tests for extraction prompt engineering module."""

import json
from dataclasses import FrozenInstanceError

import pytest

from src.core.extraction_prompt import (
    SYSTEM_PROMPT,
    ExtractionPromptBuilder,
    ExtractionResult,
    FieldDefinition,
    ListFieldDefinition,
    fields_from_blueprint_extract,
)

# ── FieldDefinition ──────────────────────────────────────────────────────────


class TestFieldDefinition:
    def test_defaults(self):
        f = FieldDefinition(name="balance")
        assert f.name == "balance"
        assert f.type == "text"
        assert f.description == ""
        assert f.sensitive is False
        assert f.example is None

    def test_full(self):
        f = FieldDefinition(
            name="balance",
            type="currency",
            description="Current balance",
            sensitive=True,
            example="$1,234.56",
        )
        assert f.type == "currency"
        assert f.sensitive is True

    def test_to_prompt_dict_minimal(self):
        f = FieldDefinition(name="balance")
        d = f.to_prompt_dict()
        assert d == {"name": "balance", "type": "text"}

    def test_to_prompt_dict_full(self):
        f = FieldDefinition(
            name="balance",
            type="currency",
            description="Current balance",
            example="$100.00",
        )
        d = f.to_prompt_dict()
        assert d == {
            "name": "balance",
            "type": "currency",
            "description": "Current balance",
            "example": "$100.00",
        }

    def test_frozen(self):
        f = FieldDefinition(name="x")
        with pytest.raises(FrozenInstanceError):
            f.name = "y"


class TestListFieldDefinition:
    def test_basic(self):
        sub = FieldDefinition(name="month", type="text")
        lf = ListFieldDefinition(
            name="history",
            description="Monthly history",
            fields=(sub,),
        )
        assert lf.name == "history"
        assert len(lf.fields) == 1

    def test_to_prompt_dict(self):
        subs = (
            FieldDefinition(name="month", type="text", description="Billing month"),
            FieldDefinition(name="cost", type="currency"),
        )
        lf = ListFieldDefinition(name="usage", description="Usage records", fields=subs)
        d = lf.to_prompt_dict()
        assert d["name"] == "usage"
        assert d["type"] == "list"
        assert d["description"] == "Usage records"
        assert len(d["fields"]) == 2
        assert d["fields"][0] == {
            "name": "month",
            "type": "text",
            "description": "Billing month",
        }

    def test_empty_fields(self):
        lf = ListFieldDefinition(name="items")
        d = lf.to_prompt_dict()
        assert d["fields"] == []


# ── ExtractionResult ──────────────────────────────────────────────────────────


class TestExtractionResult:
    def test_basic(self):
        r = ExtractionResult(
            data={"balance": 100.50},
            selectors={"balance": "span.balance"},
            confidence=0.95,
            raw_response={"data": {"balance": 100.50}},
        )
        assert r.data["balance"] == 100.50
        assert r.selectors["balance"] == "span.balance"
        assert r.confidence == 0.95

    def test_frozen(self):
        r = ExtractionResult(data={}, selectors={}, confidence=0.0, raw_response={})
        with pytest.raises(FrozenInstanceError):
            r.confidence = 1.0


# ── ExtractionPromptBuilder ──────────────────────────────────────────────────


class TestExtractionPromptBuilder:
    def setup_method(self):
        self.builder = ExtractionPromptBuilder()

    def test_system_prompt_default(self):
        assert self.builder.system_prompt == SYSTEM_PROMPT
        assert "extraction assistant" in self.builder.system_prompt

    def test_system_prompt_custom(self):
        b = ExtractionPromptBuilder(system_prompt="Custom instructions")
        assert b.system_prompt == "Custom instructions"

    def test_build_extraction_prompt_basic(self):
        fields = [
            FieldDefinition(name="balance", type="currency", description="Current balance"),
            FieldDefinition(name="account_number", type="text", description="Account ID"),
        ]
        html = '<div data-pid="1"><span class="bal">$100.00</span></div>'
        prompt = self.builder.build_extraction_prompt(html, fields)

        # Should contain field definitions
        assert '"name": "balance"' in prompt
        assert '"type": "currency"' in prompt
        assert '"description": "Current balance"' in prompt

        # Should contain output schema
        assert '"data"' in prompt
        assert '"selectors"' in prompt
        assert '"confidence"' in prompt

        # Should contain the HTML
        assert "$100.00" in prompt
        assert "```html" in prompt

    def test_build_extraction_prompt_with_context(self):
        fields = [FieldDefinition(name="balance", type="currency")]
        prompt = self.builder.build_extraction_prompt(
            "<div>$100</div>",
            fields,
            page_context="Utility bill dashboard for residential customers",
        )
        assert "Utility bill dashboard" in prompt
        assert "## Page Context" in prompt

    def test_build_extraction_prompt_without_context(self):
        fields = [FieldDefinition(name="x")]
        prompt = self.builder.build_extraction_prompt("<div>test</div>", fields)
        assert "## Page Context" not in prompt

    def test_build_extraction_prompt_list_field(self):
        sub_fields = (
            FieldDefinition(name="month", type="text"),
            FieldDefinition(name="kwh", type="number"),
            FieldDefinition(name="cost", type="currency"),
        )
        fields = [
            FieldDefinition(name="account", type="text"),
            ListFieldDefinition(name="usage", description="Monthly usage", fields=sub_fields),
        ]
        prompt = self.builder.build_extraction_prompt("<table>...</table>", fields)

        assert '"name": "usage"' in prompt
        assert '"type": "list"' in prompt
        assert '"name": "month"' in prompt

    def test_output_schema_scalar_types(self):
        fields = [
            FieldDefinition(name="text_field", type="text"),
            FieldDefinition(name="money", type="currency"),
            FieldDefinition(name="when", type="date"),
            FieldDefinition(name="flag", type="boolean"),
            FieldDefinition(name="count", type="number"),
        ]
        schema = self.builder._build_output_schema(fields)

        assert schema["data"]["text_field"] == "<text>"
        assert schema["data"]["money"] == "<number>"
        assert schema["data"]["when"] == "<ISO_date_string>"
        assert schema["data"]["flag"] == "<true/false>"
        assert schema["data"]["count"] == "<number>"
        assert schema["selectors"]["text_field"] == "<css_selector>"
        assert schema["confidence"] == "<float 0.0-1.0>"

    def test_output_schema_list_type(self):
        sub = (FieldDefinition(name="item", type="text"),)
        fields = [ListFieldDefinition(name="items", fields=sub)]
        schema = self.builder._build_output_schema(fields)

        assert isinstance(schema["data"]["items"], list)
        assert schema["data"]["items"][0] == {"item": "<text>"}
        assert schema["selectors"]["items"]["row"] == "<css_selector_for_each_row>"
        assert schema["selectors"]["items"]["fields"] == {"item": "<css_selector>"}


# ── Verification Prompt ───────────────────────────────────────────────────────


class TestVerificationPrompt:
    def test_build_selector_verification_prompt(self):
        builder = ExtractionPromptBuilder()
        prompt = builder.build_selector_verification_prompt(
            simplified_html='<div data-pid="1">$100</div>',
            selectors={"balance": "div[data-pid='1']"},
            expected_values={"balance": 100.0},
        )

        assert "Verify" in prompt
        assert "balance" in prompt
        assert "div[data-pid='1']" in prompt
        assert '"verified"' in prompt
        assert '"mismatches"' in prompt
        assert "```html" in prompt


# ── Parse Response ────────────────────────────────────────────────────────────


class TestParseResponse:
    def setup_method(self):
        self.builder = ExtractionPromptBuilder()

    def test_parse_dict(self):
        data = {
            "data": {"balance": 100.50, "account": "ACC-123"},
            "selectors": {"balance": "span.bal", "account": "#acct"},
            "confidence": 0.95,
        }
        result = self.builder.parse_response(data)
        assert result.data["balance"] == 100.50
        assert result.selectors["balance"] == "span.bal"
        assert result.confidence == 0.95
        assert result.raw_response == data

    def test_parse_string(self):
        data = json.dumps(
            {
                "data": {"x": 1},
                "selectors": {"x": ".x"},
                "confidence": 0.8,
            }
        )
        result = self.builder.parse_response(data)
        assert result.data["x"] == 1
        assert result.confidence == 0.8

    def test_parse_llm_response_object(self):
        """Parse from an object with parse_json() method."""

        class FakeLLMResponse:
            def parse_json(self):
                return {
                    "data": {"val": "test"},
                    "selectors": {"val": ".v"},
                    "confidence": 0.9,
                }

        result = self.builder.parse_response(FakeLLMResponse())
        assert result.data["val"] == "test"
        assert result.confidence == 0.9

    def test_parse_missing_fields(self):
        result = self.builder.parse_response({"some": "thing"})
        assert result.data == {}
        assert result.selectors == {}
        assert result.confidence == 0.0

    def test_parse_confidence_clamped_high(self):
        result = self.builder.parse_response({"confidence": 1.5})
        assert result.confidence == 1.0

    def test_parse_confidence_clamped_low(self):
        result = self.builder.parse_response({"confidence": -0.5})
        assert result.confidence == 0.0

    def test_parse_confidence_invalid_type(self):
        result = self.builder.parse_response({"confidence": "high"})
        assert result.confidence == 0.0

    def test_parse_not_dict_raises(self):
        with pytest.raises(ValueError, match="Expected dict"):
            self.builder.parse_response("[1, 2, 3]")

    def test_parse_invalid_json_raises(self):
        with pytest.raises(json.JSONDecodeError):
            self.builder.parse_response("not json at all")


# ── Blueprint Conversion ─────────────────────────────────────────────────────


class TestFieldsFromBlueprint:
    def test_simple_fields(self):
        config = {
            "account_number": {"type": "text", "description": "Account ID", "sensitive": True},
            "balance": {"type": "currency", "description": "Current balance"},
        }
        fields = fields_from_blueprint_extract(config)
        assert len(fields) == 2
        assert isinstance(fields[0], FieldDefinition)
        assert fields[0].name == "account_number"
        assert fields[0].type == "text"
        assert fields[0].description == "Account ID"
        assert fields[0].sensitive is True
        assert fields[1].name == "balance"
        assert fields[1].type == "currency"

    def test_list_field(self):
        config = {
            "usage_history": {
                "type": "list",
                "description": "Monthly usage records",
                "fields": {
                    "month": {"type": "text", "description": "Billing month"},
                    "kwh": {"type": "number"},
                    "cost": {"type": "currency"},
                },
            }
        }
        fields = fields_from_blueprint_extract(config)
        assert len(fields) == 1
        f = fields[0]
        assert isinstance(f, ListFieldDefinition)
        assert f.name == "usage_history"
        assert f.description == "Monthly usage records"
        assert len(f.fields) == 3
        assert f.fields[0].name == "month"
        assert f.fields[0].description == "Billing month"
        assert f.fields[1].name == "kwh"
        assert f.fields[1].type == "number"

    def test_mixed_fields(self):
        config = {
            "account": {"type": "text"},
            "transactions": {
                "type": "table",
                "fields": {
                    "date": {"type": "date"},
                    "amount": {"type": "currency"},
                },
            },
        }
        fields = fields_from_blueprint_extract(config)
        assert len(fields) == 2
        assert isinstance(fields[0], FieldDefinition)
        assert isinstance(fields[1], ListFieldDefinition)

    def test_with_example(self):
        config = {
            "phone": {"type": "phone", "example": "(555) 123-4567"},
        }
        fields = fields_from_blueprint_extract(config)
        assert fields[0].example == "(555) 123-4567"

    def test_empty_config(self):
        assert fields_from_blueprint_extract({}) == []

    def test_invalid_entries_skipped(self):
        config = {
            "valid": {"type": "text"},
            "invalid_string": "not a dict",
            "invalid_number": 42,
        }
        fields = fields_from_blueprint_extract(config)
        assert len(fields) == 1
        assert fields[0].name == "valid"

    def test_defaults_for_missing_keys(self):
        config = {
            "minimal": {},  # No type, no description
        }
        fields = fields_from_blueprint_extract(config)
        assert len(fields) == 1
        assert fields[0].type == "text"
        assert fields[0].description == ""
        assert fields[0].sensitive is False


# ── Integration-style: Full Prompt Round-trip ─────────────────────────────────


class TestFullPromptRoundtrip:
    """Test the full flow: build prompt → simulate LLM response → parse."""

    def test_extraction_roundtrip(self):
        builder = ExtractionPromptBuilder()
        fields = [
            FieldDefinition(name="account_number", type="text", description="Account ID"),
            FieldDefinition(name="balance", type="currency", description="Amount owed"),
        ]

        html = """
        <div data-pid="1">
            <h1>Account Dashboard</h1>
            <div data-pid="2">
                <span class="acct" data-pid="3">ACC-12345</span>
                <div class="balance" data-pid="4">$1,234.56</div>
            </div>
        </div>
        """

        builder.build_extraction_prompt(html, fields, page_context="Utility bill dashboard")

        # Simulate LLM response
        llm_output = {
            "data": {
                "account_number": "ACC-12345",
                "balance": 1234.56,
            },
            "selectors": {
                "account_number": 'span.acct[data-pid="3"]',
                "balance": 'div.balance[data-pid="4"]',
            },
            "confidence": 0.95,
        }

        result = builder.parse_response(llm_output)
        assert result.data["account_number"] == "ACC-12345"
        assert result.data["balance"] == 1234.56
        assert result.confidence == 0.95
        assert "data-pid" in result.selectors["account_number"]

    def test_list_extraction_roundtrip(self):
        builder = ExtractionPromptBuilder()
        fields = [
            ListFieldDefinition(
                name="transactions",
                description="Recent transactions",
                fields=(
                    FieldDefinition(name="date", type="date"),
                    FieldDefinition(name="amount", type="currency"),
                    FieldDefinition(name="description", type="text"),
                ),
            )
        ]

        builder.build_extraction_prompt("<table>...</table>", fields)

        llm_output = {
            "data": {
                "transactions": [
                    {"date": "2024-01-15", "amount": 42.50, "description": "Payment"},
                    {"date": "2024-02-15", "amount": 45.00, "description": "Payment"},
                ]
            },
            "selectors": {
                "transactions": {
                    "row": "table tbody tr",
                    "fields": {
                        "date": "td:nth-child(1)",
                        "amount": "td:nth-child(2)",
                        "description": "td:nth-child(3)",
                    },
                }
            },
            "confidence": 0.88,
        }

        result = builder.parse_response(llm_output)
        assert len(result.data["transactions"]) == 2
        assert result.data["transactions"][0]["amount"] == 42.50
        assert result.confidence == 0.88
