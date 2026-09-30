"""
Data Extractor — extracts and normalizes data from pages using blueprint definitions.

Provides:
- Typed extraction (text, currency, date, number, etc.)
- Built-in transforms (strip_whitespace, parse_date, regex_extract, etc.)
- List/table extraction with row iteration
- Per-field errors: a missing field comes back as null (or its default) instead
  of discarding everything else, unless the blueprint marks it required
- Sensitive field handling (values are never logged)
- Pagination, with every "next" click checked by the read-only policy
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from src.core.blueprint import (
    ExtractionField,
    FieldType,
    ListExtractionField,
    TransformType,
)
from src.core.read_only_policy import ReadOnlyExecutionPolicy
from src.exceptions import DataExtractionError
from src.logging_config import get_logger

logger = get_logger("data_extractor")

# The first field may wait this long for the page to render its data…
DEFAULT_FIELD_TIMEOUT_MS = 15000
# …after which the page has settled, and an absent field is absent.
SETTLED_FIELD_TIMEOUT_MS = 3000


# ── Transform Functions ──────────────────────────────────────────────────────


def transform_strip_whitespace(value: str) -> str:
    """Remove leading/trailing whitespace and collapse internal spaces."""
    return " ".join(value.split())


def transform_strip_dollar_sign(value: str) -> str:
    """Remove dollar signs and surrounding whitespace."""
    return value.replace("$", "").strip()


def transform_strip_commas(value: str) -> str:
    """Remove commas from numbers."""
    return value.replace(",", "")


def transform_to_lowercase(value: str) -> str:
    return value.lower()


def transform_to_uppercase(value: str) -> str:
    return value.upper()


# A number as written on a statement: digits with optional grouping (, . space
# ' or thin/narrow spaces) and an optional decimal part.
_NUMBER_TOKEN = re.compile("\\d[\\d.,'\u00a0\u202f\u2009 ]*")
_GROUP_SEPARATORS = re.compile("['\u00a0\u202f\u2009 ]")
_NEGATIVE_MARKERS = ("-", "\u2212", "\u2013")
_CREDIT_SUFFIX = re.compile(r"CR\b", re.IGNORECASE)


def _normalize_number(token: str) -> Optional[str]:
    """Turn '1,234.56' / '1.234,56' / '1 234,56' into '1234.56'; None if ambiguous garbage."""
    token = _GROUP_SEPARATORS.sub("", token).rstrip(".,")
    if not token:
        return None
    last_dot, last_comma = token.rfind("."), token.rfind(",")
    if last_dot >= 0 and last_comma >= 0:
        decimal = "." if last_dot > last_comma else ","
        group = "," if decimal == "." else "."
        integer, _, fraction = token.rpartition(decimal)
        integer = integer.replace(group, "")
        if not integer.isdigit() or not fraction.isdigit():
            return None
        return f"{integer}.{fraction}"
    for separator in (",", "."):
        if separator not in token:
            continue
        parts = token.split(separator)
        if len(parts) > 2 or (len(parts) == 2 and len(parts[1]) == 3 and separator == ","):
            # Repeated, or a single ',' followed by three digits: grouping ("1,234", "1.234.567").
            if all(part.isdigit() for part in parts) and all(len(part) == 3 for part in parts[1:]):
                return "".join(parts)
            return None
        # One separator: a decimal point ("12.50", "12,50").
        integer, fraction = parts
        if not integer.isdigit() or not fraction.isdigit():
            return None
        return f"{integer}.{fraction}"
    return token if token.isdigit() else None


def parse_number(value: Any) -> Optional[float]:
    """Parse the first number in a statement-style string, or None when there is none.

    Handles grouping and decimal separators in either convention ("1,234.56",
    "1.234,56 €", "1 234,56"), a leading or trailing minus, and accounting
    parentheses for negatives ("(1,234.56)"). Returns None — never a made-up
    0.0 — for "N/A", "--", "Pending" and other text without a number.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    match = _NUMBER_TOKEN.search(text)
    if not match:
        return None
    normalized = _normalize_number(match.group(0))
    if normalized is None:
        return None
    try:
        number = float(normalized)
    except ValueError:
        return None

    before = text[: match.start()].rstrip(" $€£¥₹\u00a0").rstrip()
    after = text[match.end() :].lstrip(" $€£¥₹\u00a0").lstrip()
    negative = (
        before.endswith(_NEGATIVE_MARKERS)
        or after.startswith(_NEGATIVE_MARKERS)
        or (before.endswith("(") and after.startswith(")"))
        or bool(_CREDIT_SUFFIX.match(after))  # statements mark credits as "123.45 CR"
    )
    return -number if negative else number


def transform_to_number(value: str) -> Optional[float]:
    """Parse a string to a number; None when it holds no number."""
    return parse_number(value)


def transform_to_currency(value: str) -> Optional[float]:
    """Parse a currency string to a float rounded to cents; None when it holds no amount."""
    number = parse_number(value)
    return None if number is None else round(number, 2)


def transform_parse_date(value: str, fmt: Optional[str] = None) -> str:
    """Parse a date string and return ISO format."""
    if fmt:
        try:
            dt = datetime.strptime(value.strip(), fmt)
            return dt.isoformat()
        except ValueError:
            pass

    # Try common formats
    formats = [
        "%m/%d/%Y",
        "%Y-%m-%d",
        "%m-%d-%Y",
        "%d/%m/%Y",
        "%B %d, %Y",
        "%b %d, %Y",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%m/%d/%y",
    ]
    for f in formats:
        try:
            dt = datetime.strptime(value.strip(), f)
            return dt.isoformat()
        except ValueError:
            continue

    # Return as-is if no format matches
    return value.strip()


def transform_regex_extract(value: str, pattern: str) -> str:
    """Extract the first regex match group from a value."""
    match = re.search(pattern, value)
    if match:
        return match.group(1) if match.groups() else match.group(0)
    return value


TRANSFORMS = {
    TransformType.STRIP_WHITESPACE: transform_strip_whitespace,
    TransformType.STRIP_DOLLAR_SIGN: transform_strip_dollar_sign,
    TransformType.STRIP_COMMAS: transform_strip_commas,
    TransformType.TO_LOWERCASE: transform_to_lowercase,
    TransformType.TO_UPPERCASE: transform_to_uppercase,
    TransformType.TO_NUMBER: transform_to_number,
    TransformType.TO_CURRENCY: transform_to_currency,
    TransformType.PARSE_DATE: transform_parse_date,
    "strip_whitespace": transform_strip_whitespace,
    "strip_dollar_sign": transform_strip_dollar_sign,
    "strip_commas": transform_strip_commas,
    "to_lowercase": transform_to_lowercase,
    "to_uppercase": transform_to_uppercase,
    "to_number": transform_to_number,
    "to_currency": transform_to_currency,
    "parse_date": transform_parse_date,
}


def apply_transform(value: str, transform: Union[TransformType, str, None]) -> Any:
    """
    Apply a transform function to a raw extracted value.

    Args:
        value: The raw string value.
        transform: Transform name or enum, optionally with args (e.g., "regex_extract(\\d+)").

    Returns:
        Transformed value.
    """
    if transform is None:
        return value

    transform_str = transform.value if isinstance(transform, TransformType) else str(transform)

    # Handle parameterized transforms: "transform_name(arg)"
    param_match = re.match(r"(\w+)\((.+)\)", transform_str)
    if param_match:
        func_name = param_match.group(1)
        param = param_match.group(2)

        if func_name == "regex_extract":
            return transform_regex_extract(value, param)
        if func_name == "parse_date":
            return transform_parse_date(value, param)

    # Simple transforms
    func = TRANSFORMS.get(transform_str) or TRANSFORMS.get(transform)
    if func:
        return func(value)

    logger.warning(f"Unknown transform: {transform_str}, returning raw value")
    return value


def coerce_type(value: Any, field_type: FieldType) -> Any:
    """
    Coerce a value to the specified field type.

    Args:
        value: The value to coerce (usually a string).
        field_type: The target type.

    Returns:
        Typed value, or None when a currency/number field holds no number.
    """
    if value is None:
        return None

    if (
        field_type in (FieldType.CURRENCY, FieldType.NUMBER)
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
    ):
        return round(float(value), 2) if field_type == FieldType.CURRENCY else float(value)

    str_val = str(value).strip()

    if field_type == FieldType.TEXT:
        return str_val
    if field_type == FieldType.CURRENCY:
        return transform_to_currency(str_val)
    if field_type == FieldType.NUMBER:
        return transform_to_number(str_val)
    if field_type == FieldType.DATE:
        return transform_parse_date(str_val)
    if field_type == FieldType.EMAIL:
        return str_val.lower().strip()
    if field_type == FieldType.PHONE:
        return re.sub(r"[^\d+\-() ]", "", str_val)
    if field_type == FieldType.BOOLEAN:
        return str_val.lower() in ("true", "yes", "1", "on", "active")
    return str_val


def _default_for(field_def: ExtractionField) -> Any:
    """A field's declared default, in the field's own type."""
    if field_def.default is None:
        return None
    return coerce_type(field_def.default, field_def.type)


# ── Page Extractor ────────────────────────────────────────────────────────────


_DESCRIBE_ELEMENT_JS = """(element) => {
    const form = element.closest('form');
    return {
        text: element.innerText || element.textContent || '',
        ariaLabel: element.getAttribute('aria-label') || '',
        title: element.getAttribute('title') || '',
        value: element.getAttribute('value') || '',
        name: element.getAttribute('name') || '',
        id: element.id || '',
        href: element.getAttribute('href') || '',
        formAction: element.getAttribute('formaction') || form?.getAttribute('action') || '',
        formMethod: form?.getAttribute('method') || '',
    };
}"""


class DataExtractor:
    """
    Extracts structured data from a Playwright Page using blueprint extraction definitions.

    Usage:
        extractor = DataExtractor(page)
        data = await extractor.extract(blueprint.extract)
        extractor.field_errors  # {"field": "why it has no value"}
    """

    def __init__(self, page: Page, *, read_only_policy: Optional[ReadOnlyExecutionPolicy] = None) -> None:
        self.page = page
        self.read_only_policy = read_only_policy
        self.field_errors: Dict[str, str] = {}
        self._settled = False

    async def extract(
        self,
        fields: Dict[str, Union[ExtractionField, ListExtractionField]],
        site: str = "unknown",
    ) -> Dict[str, Any]:
        """
        Extract all defined fields from the current page.

        A field that cannot be read comes back as its default (or None) and is
        listed in ``field_errors``; the other fields are still returned.

        Args:
            fields: Dict mapping field names to extraction configs.
            site: Site identifier for error messages.

        Returns:
            Dict of extracted, typed, transformed data.

        Raises:
            DataExtractionError: when a field marked ``required`` has no value.
        """
        result: Dict[str, Any] = {}
        self.field_errors = {}

        for name, field_def in fields.items():
            try:
                if isinstance(field_def, ListExtractionField):
                    result[name] = await self._extract_list(field_def, name, site)
                else:
                    result[name] = await self._extract_field(field_def, name, site)
            except Exception as e:
                reason = str(e).splitlines()[0] if str(e) else type(e).__name__
                logger.warning(
                    f"Extraction failed for {name}",
                    extra={"extra_data": {"field": name, "error": reason}},
                )
                self.field_errors.setdefault(name, reason)
                result[name] = _default_for(field_def) if isinstance(field_def, ExtractionField) else None

            # Values are never logged; sensitive ones not even by name at debug level.
            if not (isinstance(field_def, ExtractionField) and field_def.sensitive):
                logger.debug(
                    f"Extracted {name}",
                    extra={"extra_data": {"field": name, "type": field_def.type.value}},
                )

        missing_required = [
            name
            for name, field_def in fields.items()
            if field_def.required and (result.get(name) is None or result.get(name) == [])
        ]
        if missing_required:
            raise DataExtractionError(
                site=site,
                detail=f"Required field(s) not found: {', '.join(missing_required)}.",
            )

        return result

    def _field_timeout(self, field_def: ExtractionField) -> int:
        if field_def.timeout:
            return field_def.timeout
        return SETTLED_FIELD_TIMEOUT_MS if self._settled else DEFAULT_FIELD_TIMEOUT_MS

    async def _extract_field(
        self,
        field_def: ExtractionField,
        name: str,
        site: str,
    ) -> Any:
        """Extract a single field value."""
        try:
            element = await self.page.wait_for_selector(
                field_def.selector, timeout=self._field_timeout(field_def), state="attached"
            )
        except PlaywrightTimeout:
            self._settled = True
            if field_def.default is None:
                self.field_errors[name] = "not_found"
            return _default_for(field_def)
        self._settled = True

        if element is None:
            if field_def.default is None:
                self.field_errors[name] = "not_found"
            return _default_for(field_def)

        # Get the raw value
        if field_def.attribute:
            raw_value = await element.get_attribute(field_def.attribute) or ""
        else:
            raw_value = await element.inner_text()

        # Apply transform
        value = apply_transform(raw_value, field_def.transform)

        # Coerce to type
        value = coerce_type(value, field_def.type)
        if value is None:
            if field_def.default is not None:
                return _default_for(field_def)
            self.field_errors[name] = "unparseable"

        return value

    async def _next_page_ready(self, next_btn: Any, selector: str) -> bool:
        """Whether the pagination control can and may be clicked."""
        try:
            if not await next_btn.is_visible():
                return False
            if await next_btn.is_disabled():
                return False
            aria_disabled = await next_btn.get_attribute("aria-disabled")
            if aria_disabled and aria_disabled.strip().lower() == "true":
                return False
        except Exception:
            return False

        if self.read_only_policy is not None:
            try:
                metadata = await next_btn.evaluate(_DESCRIBE_ELEMENT_JS)
            except Exception:
                metadata = {}
            reason = self.read_only_policy.evaluate_click(selector, metadata if isinstance(metadata, dict) else {})
            if reason:
                self.read_only_policy.record_blocked("pagination_click", reason, target=None)
                logger.warning("Pagination stopped: the next control failed the read-only click policy")
                return False
        return True

    async def _extract_list(
        self,
        field_def: ListExtractionField,
        name: str,
        site: str,
    ) -> List[Dict[str, Any]]:
        """Extract a list of items (e.g., transaction rows)."""
        items: List[Dict[str, Any]] = []
        page_num = 0
        max_pages = 1

        if field_def.pagination:
            max_pages = field_def.pagination.max_pages

        while page_num < max_pages:
            # Get all row elements
            rows = await self.page.query_selector_all(field_def.selector)
            self._settled = True

            if not rows:
                if page_num == 0:
                    self.field_errors[name] = "not_found"
                break

            max_items = field_def.max_items
            for i, row in enumerate(rows):
                if max_items and len(items) >= max_items:
                    break

                row_data: Dict[str, Any] = {}
                for col_name, col_def in field_def.fields.items():
                    try:
                        cell = await row.query_selector(col_def.selector)
                        if cell:
                            if col_def.attribute:
                                raw = await cell.get_attribute(col_def.attribute) or ""
                            else:
                                raw = await cell.inner_text()

                            value = apply_transform(raw, col_def.transform)
                            value = coerce_type(value, col_def.type)
                            row_data[col_name] = _default_for(col_def) if value is None else value
                        else:
                            row_data[col_name] = _default_for(col_def)
                    except Exception as e:
                        logger.debug(
                            f"Column extraction failed: {col_name} in row {i}",
                            extra={"extra_data": {"error": str(e).splitlines()[0] if str(e) else type(e).__name__}},
                        )
                        row_data[col_name] = _default_for(col_def)

                items.append(row_data)

            if max_items and len(items) >= max_items:
                break

            # Handle pagination
            if field_def.pagination and page_num < max_pages - 1:
                try:
                    next_selector = field_def.pagination.next_selector
                    next_btn = await self.page.query_selector(next_selector)
                    if next_btn is None or not await self._next_page_ready(next_btn, next_selector):
                        break
                    await next_btn.click()
                    await self.page.wait_for_timeout(field_def.pagination.wait_after_click)
                except Exception:
                    break

            page_num += 1

        logger.debug(
            f"Extracted {len(items)} items for {name}",
            extra={"extra_data": {"field": name, "count": len(items)}},
        )
        return items
