"""
Data Extractor — extracts and normalizes data from pages using blueprint definitions.

Provides:
- Typed extraction (text, currency, date, number, etc.)
- Built-in transforms (strip_whitespace, parse_date, regex_extract, etc.)
- List/table extraction with row iteration
- Sensitive field handling (marked fields are never logged)
- Pagination support
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from src.core.blueprint import (
    ExtractionField,
    FieldType,
    ListExtractionField,
    TransformType,
)
from src.exceptions import DataExtractionError
from src.logging_config import get_logger

logger = get_logger("data_extractor")


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


def transform_to_number(value: str) -> float:
    """Parse a string to a number, stripping non-numeric chars except . and -."""
    cleaned = re.sub(r"[^\d.\-]", "", value)
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def transform_to_currency(value: str) -> float:
    """Parse a currency string to a float."""
    cleaned = re.sub(r"[^\d.\-]", "", value)
    try:
        return round(float(cleaned), 2)
    except ValueError:
        return 0.0


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
        Typed value.
    """
    if value is None:
        return None

    str_val = str(value).strip()

    match field_type:
        case FieldType.TEXT:
            return str_val
        case FieldType.CURRENCY:
            return transform_to_currency(str_val)
        case FieldType.NUMBER:
            return transform_to_number(str_val)
        case FieldType.DATE:
            return transform_parse_date(str_val)
        case FieldType.EMAIL:
            return str_val.lower().strip()
        case FieldType.PHONE:
            return re.sub(r"[^\d+\-() ]", "", str_val)
        case FieldType.BOOLEAN:
            return str_val.lower() in ("true", "yes", "1", "on", "active")
        case _:
            return str_val


# ── Page Extractor ────────────────────────────────────────────────────────────


class DataExtractor:
    """
    Extracts structured data from a Playwright Page using blueprint extraction definitions.

    Usage:
        extractor = DataExtractor(page)
        data = await extractor.extract(blueprint.extract)
    """

    def __init__(self, page: Page) -> None:
        self.page = page

    async def extract(
        self,
        fields: Dict[str, Union[ExtractionField, ListExtractionField]],
        site: str = "unknown",
    ) -> Dict[str, Any]:
        """
        Extract all defined fields from the current page.

        Args:
            fields: Dict mapping field names to extraction configs.
            site: Site identifier for error messages.

        Returns:
            Dict of extracted, typed, transformed data.
        """
        result: Dict[str, Any] = {}

        for name, field_def in fields.items():
            try:
                if isinstance(field_def, ListExtractionField):
                    result[name] = await self._extract_list(field_def, name, site)
                else:
                    result[name] = await self._extract_field(field_def, name, site)

                # Log non-sensitive fields
                if not (isinstance(field_def, ExtractionField) and field_def.sensitive):
                    logger.debug(
                        f"Extracted {name}",
                        extra={"extra_data": {"field": name, "type": field_def.type.value}},
                    )
            except DataExtractionError:
                raise
            except Exception as e:
                logger.warning(
                    f"Extraction failed for {name}: {e}",
                    extra={"extra_data": {"field": name, "error": str(e)}},
                )
                # Use default if available
                if isinstance(field_def, ExtractionField) and field_def.default is not None:
                    result[name] = field_def.default
                else:
                    result[name] = None

        return result

    async def _extract_field(
        self,
        field_def: ExtractionField,
        name: str,
        site: str,
    ) -> Any:
        """Extract a single field value."""
        try:
            element = await self.page.wait_for_selector(
                field_def.selector, timeout=5000, state="attached"
            )
        except PlaywrightTimeout:
            if field_def.default is not None:
                return field_def.default
            raise DataExtractionError(
                site=site,
                detail=f"Selector '{field_def.selector}' not found for field '{name}'.",
            )

        if element is None:
            if field_def.default is not None:
                return field_def.default
            return None

        # Get the raw value
        if field_def.attribute:
            raw_value = await element.get_attribute(field_def.attribute) or ""
        else:
            raw_value = await element.inner_text()

        # Apply transform
        value = apply_transform(raw_value, field_def.transform)

        # Coerce to type
        value = coerce_type(value, field_def.type)

        return value

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

            if not rows:
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
                            row_data[col_name] = value
                        else:
                            row_data[col_name] = col_def.default
                    except Exception as e:
                        logger.debug(
                            f"Column extraction failed: {col_name} in row {i}",
                            extra={"extra_data": {"error": str(e)}},
                        )
                        row_data[col_name] = col_def.default

                items.append(row_data)

            # Handle pagination
            if field_def.pagination and page_num < max_pages - 1:
                try:
                    next_btn = await self.page.query_selector(
                        field_def.pagination.next_selector
                    )
                    if next_btn:
                        is_disabled = await next_btn.get_attribute("disabled")
                        if is_disabled:
                            break
                        await next_btn.click()
                        await self.page.wait_for_timeout(
                            field_def.pagination.wait_after_click
                        )
                    else:
                        break
                except Exception:
                    break

            page_num += 1

        logger.debug(
            f"Extracted {len(items)} items for {name}",
            extra={"extra_data": {"field": name, "count": len(items)}},
        )
        return items
