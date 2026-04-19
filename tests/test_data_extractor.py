"""
Tests for Data Extractor — transforms, type coercion, and extraction logic.
"""

from src.core.blueprint import FieldType, TransformType
from src.core.data_extractor import (
    apply_transform,
    coerce_type,
    transform_parse_date,
    transform_regex_extract,
    transform_strip_commas,
    transform_strip_dollar_sign,
    transform_strip_whitespace,
    transform_to_currency,
    transform_to_lowercase,
    transform_to_number,
    transform_to_uppercase,
)

# ── Transform Tests ───────────────────────────────────────────────────────────


class TestTransforms:
    def test_strip_whitespace(self):
        assert transform_strip_whitespace("  hello   world  ") == "hello world"
        assert transform_strip_whitespace("no extra") == "no extra"

    def test_strip_dollar_sign(self):
        assert transform_strip_dollar_sign("$1,234.56") == "1,234.56"
        assert transform_strip_dollar_sign("$0.99") == "0.99"
        assert transform_strip_dollar_sign("100") == "100"

    def test_strip_commas(self):
        assert transform_strip_commas("1,234,567") == "1234567"
        assert transform_strip_commas("100") == "100"

    def test_to_lowercase(self):
        assert transform_to_lowercase("HELLO World") == "hello world"

    def test_to_uppercase(self):
        assert transform_to_uppercase("hello") == "HELLO"

    def test_to_number(self):
        assert transform_to_number("$1,234.56") == 1234.56
        assert transform_to_number("-42.5") == -42.5
        assert transform_to_number("abc") == 0.0

    def test_to_currency(self):
        assert transform_to_currency("$4,521.30") == 4521.30
        assert transform_to_currency("-$127.45") == -127.45
        assert transform_to_currency("abc") == 0.0

    def test_parse_date_iso(self):
        result = transform_parse_date("2026-03-14")
        assert "2026-03-14" in result

    def test_parse_date_us_format(self):
        result = transform_parse_date("03/14/2026")
        assert "2026-03-14" in result

    def test_parse_date_with_format(self):
        result = transform_parse_date("March 14, 2026", "%B %d, %Y")
        assert "2026-03-14" in result

    def test_parse_date_unknown_returns_raw(self):
        result = transform_parse_date("not a date")
        assert result == "not a date"

    def test_regex_extract(self):
        result = transform_regex_extract("Account #12345", r"#(\d+)")
        assert result == "12345"

    def test_regex_extract_no_match(self):
        result = transform_regex_extract("no numbers", r"(\d+)")
        assert result == "no numbers"


# ── Apply Transform Tests ────────────────────────────────────────────────────


class TestApplyTransform:
    def test_none_transform(self):
        assert apply_transform("hello", None) == "hello"

    def test_enum_transform(self):
        assert apply_transform("  spaces  ", TransformType.STRIP_WHITESPACE) == "spaces"

    def test_string_transform(self):
        assert apply_transform("$99.99", "strip_dollar_sign") == "99.99"

    def test_parameterized_regex(self):
        result = apply_transform("ID: 42", "regex_extract(\\d+)")
        assert result == "42"

    def test_parameterized_parse_date(self):
        result = apply_transform("14-Mar-2026", "parse_date(%d-%b-%Y)")
        assert "2026-03-14" in result

    def test_unknown_transform_returns_raw(self):
        assert apply_transform("value", "nonexistent_transform") == "value"


# ── Type Coercion Tests ──────────────────────────────────────────────────────


class TestCoerceType:
    def test_text(self):
        assert coerce_type("hello", FieldType.TEXT) == "hello"

    def test_currency(self):
        assert coerce_type("$1,234.56", FieldType.CURRENCY) == 1234.56

    def test_number(self):
        assert coerce_type("42.5", FieldType.NUMBER) == 42.5

    def test_date(self):
        result = coerce_type("03/14/2026", FieldType.DATE)
        assert "2026-03-14" in result

    def test_email(self):
        assert coerce_type("John@Example.COM", FieldType.EMAIL) == "john@example.com"

    def test_phone(self):
        result = coerce_type("(555) 123-4567", FieldType.PHONE)
        assert "555" in result
        assert "123" in result

    def test_boolean_true(self):
        assert coerce_type("true", FieldType.BOOLEAN) is True
        assert coerce_type("yes", FieldType.BOOLEAN) is True
        assert coerce_type("active", FieldType.BOOLEAN) is True

    def test_boolean_false(self):
        assert coerce_type("false", FieldType.BOOLEAN) is False
        assert coerce_type("no", FieldType.BOOLEAN) is False

    def test_none_value(self):
        assert coerce_type(None, FieldType.TEXT) is None
