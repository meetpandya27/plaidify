"""
Tests for Data Extractor — transforms, type coercion, and extraction logic.
"""

import time

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeout

from src.core.blueprint import ExtractionField, FieldType, ListExtractionField, PaginationConfig, TransformType
from src.core.data_extractor import (
    DataExtractor,
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
        assert transform_to_number("abc") is None

    def test_to_currency(self):
        assert transform_to_currency("$4,521.30") == 4521.30
        assert transform_to_currency("-$127.45") == -127.45
        assert transform_to_currency("abc") is None

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


# ── Numbers that are not there stay not there (ENG-17) ───────────────────────


class TestNumberParsing:
    @pytest.mark.parametrize("raw", ["N/A", "--", "Pending", "", "   ", "no balance"])
    def test_text_without_a_number_is_none_not_zero(self, raw):
        assert coerce_type(raw, FieldType.CURRENCY) is None
        assert coerce_type(raw, FieldType.NUMBER) is None

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("(1,234.56)", -1234.56),
            ("-$127.45", -127.45),
            ("\u2212$42.10", -42.10),
            ("1,234.56-", -1234.56),
            ("123.45 CR", -123.45),
            ("$25.00 credit applied", 25.00),
            ("+$3,200.00", 3200.00),
            ("1.234,56 \u20ac", 1234.56),
            ("1 234,56", 1234.56),
            ("1\u00a0234,56", 1234.56),
            ("12,50", 12.50),
            ("1,234", 1234.0),
            ("1.234.567", 1234567.0),
            ("$1,234.56 due 10/01", 1234.56),
            ("$142.57", 142.57),
        ],
    )
    def test_statement_style_amounts(self, raw, expected):
        assert coerce_type(raw, FieldType.CURRENCY) == pytest.approx(expected)

    def test_numbers_from_an_llm_pass_through(self):
        assert coerce_type(1234.567, FieldType.CURRENCY) == 1234.57
        assert coerce_type(3, FieldType.NUMBER) == 3.0


# ── Per-field extraction (ENG-18) ─────────────────────────────────────────────


class _Element:
    def __init__(self, text):
        self._text = text

    async def inner_text(self):
        return self._text

    async def get_attribute(self, name):
        return None


class FieldPage:
    """wait_for_selector finds only the selectors in ``present``; records the timeouts it was given."""

    def __init__(self, present):
        self.present = present
        self.timeouts = []

    async def wait_for_selector(self, selector, timeout=None, state=None):
        self.timeouts.append(timeout)
        if selector in self.present:
            return _Element(self.present[selector])
        raise PlaywrightTimeout(f"Timeout {timeout}ms exceeded")


class TestPerFieldExtraction:
    @pytest.mark.asyncio
    async def test_a_missing_field_does_not_discard_the_others(self):
        page = FieldPage({"#balance": "$10.00"})
        extractor = DataExtractor(page)
        data = await extractor.extract(
            {
                "balance": ExtractionField(selector="#balance", type=FieldType.CURRENCY),
                "due": ExtractionField(selector="#due", type=FieldType.DATE),
            },
            site="bank",
        )
        assert data == {"balance": 10.0, "due": None}
        assert extractor.field_errors == {"due": "not_found"}

    @pytest.mark.asyncio
    async def test_required_fields_fail_the_extraction(self):
        from src.exceptions import DataExtractionError

        with pytest.raises(DataExtractionError, match="due"):
            await DataExtractor(FieldPage({})).extract(
                {"due": ExtractionField(selector="#due", required=True)},
                site="bank",
            )

    @pytest.mark.asyncio
    async def test_defaults_are_used_and_typed(self):
        page = FieldPage({})
        data = await DataExtractor(page).extract(
            {"overdue": ExtractionField(selector="#overdue", type=FieldType.CURRENCY, default="0.00")},
            site="bank",
        )
        assert data == {"overdue": 0.0}

    @pytest.mark.asyncio
    async def test_unparseable_amount_is_reported_not_zero(self):
        extractor = DataExtractor(FieldPage({"#balance": "Pending"}))
        data = await extractor.extract({"balance": ExtractionField(selector="#balance", type=FieldType.CURRENCY)})
        assert data == {"balance": None}
        assert extractor.field_errors == {"balance": "unparseable"}

    @pytest.mark.asyncio
    async def test_absent_fields_do_not_each_wait_the_full_timeout(self):
        page = FieldPage({"#a": "x"})
        await DataExtractor(page).extract(
            {
                "a": ExtractionField(selector="#a"),
                "b": ExtractionField(selector="#b"),
                "c": ExtractionField(selector="#c"),
                "d": ExtractionField(selector="#d", timeout=500),
            }
        )
        from src.core.data_extractor import DEFAULT_FIELD_TIMEOUT_MS, SETTLED_FIELD_TIMEOUT_MS

        assert page.timeouts == [DEFAULT_FIELD_TIMEOUT_MS, SETTLED_FIELD_TIMEOUT_MS, SETTLED_FIELD_TIMEOUT_MS, 500]


# ── Pagination (ENG-06, ENG-21) ───────────────────────────────────────────────


class _Cell(_Element):
    pass


class _Row:
    def __init__(self, value):
        self.value = value

    async def query_selector(self, selector):
        return _Cell(self.value)


class _NextButton:
    def __init__(self, *, disabled=False, aria_disabled=None, text="Next"):
        self.disabled = disabled
        self.aria_disabled = aria_disabled
        self.text = text
        self.clicks = 0

    async def is_visible(self):
        return True

    async def is_disabled(self):
        return self.disabled

    async def get_attribute(self, name):
        return self.aria_disabled if name == "aria-disabled" else None

    async def evaluate(self, script):
        return {"text": self.text}

    async def click(self):
        self.clicks += 1


class PagedPage:
    def __init__(self, next_button):
        self.next_button = next_button
        self.waits = 0

    async def query_selector_all(self, selector):
        return [_Row("a"), _Row("b")]

    async def query_selector(self, selector):
        return self.next_button

    async def wait_for_timeout(self, ms):
        self.waits += 1


def _rows(max_pages=3):
    return ListExtractionField(
        selector=".row",
        type=FieldType.LIST,
        fields={"v": ExtractionField(selector=".v")},
        pagination=PaginationConfig(next_selector="#next", max_pages=max_pages, wait_after_click=0),
    )


class TestPagination:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("button", [_NextButton(disabled=True), _NextButton(aria_disabled="true")])
    async def test_disabled_next_stops_without_clicking(self, button):
        started = time.monotonic()
        data = await DataExtractor(PagedPage(button)).extract({"rows": _rows()})
        assert len(data["rows"]) == 2
        assert button.clicks == 0
        assert time.monotonic() - started < 2

    @pytest.mark.asyncio
    async def test_enabled_next_is_followed(self):
        button = _NextButton()
        data = await DataExtractor(PagedPage(button)).extract({"rows": _rows(max_pages=3)})
        assert button.clicks == 2
        assert len(data["rows"]) == 6

    @pytest.mark.asyncio
    async def test_a_risky_next_control_is_refused_by_the_policy(self):
        from src.core.read_only_policy import ExecutionPhase, ReadOnlyExecutionPolicy

        policy = ReadOnlyExecutionPolicy(enabled=True, phase=ExecutionPhase.READ)
        button = _NextButton(text="Continue to transfer")
        data = await DataExtractor(PagedPage(button), read_only_policy=policy).extract({"rows": _rows()})
        assert button.clicks == 0
        assert len(data["rows"]) == 2
        assert policy.blocked_actions[-1].action == "pagination_click"
