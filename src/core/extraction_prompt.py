"""
Prompt Engineering — structured extraction prompts with field definitions + JSON output schema.

Builds prompts that guide an LLM to extract structured data from simplified HTML.
Returns both extracted values and CSS selectors for caching.

Usage:
    from src.core.extraction_prompt import ExtractionPromptBuilder
    builder = ExtractionPromptBuilder()
    prompt = builder.build_extraction_prompt(simplified_dom, field_defs)
    # Send prompt to LLM provider
    response = await provider.extract(prompt, system_prompt=builder.system_prompt)
    result = builder.parse_response(response)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.logging_config import get_logger

logger = get_logger("extraction_prompt")

# ── System Prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a data extraction assistant. Your job is to extract structured data from HTML pages.

Rules:
1. Extract ONLY the fields requested — do not invent data.
2. For each field, also return the CSS selector that targets the element containing the value.
3. If a field cannot be found, set its value to null and selector to null.
4. Return ONLY valid JSON matching the exact schema provided — no explanations or commentary.
5. Selectors should be as specific as possible. Prefer selectors using id, data-pid, or unique class names.
6. For list/table fields, return an array of objects and a selector for the row container.
7. Apply the type coercion described for each field (e.g., currency → number, date → ISO format).
8. Never include sensitive data in explanations — only in the designated value fields."""

# ── Data Classes ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FieldDefinition:
    """A field the LLM should extract from the page."""

    name: str
    type: str = "text"
    description: str = ""
    sensitive: bool = False
    example: Optional[str] = None

    def to_prompt_dict(self) -> Dict[str, Any]:
        """Convert to a dict suitable for prompt inclusion."""
        d: Dict[str, Any] = {"name": self.name, "type": self.type}
        if self.description:
            d["description"] = self.description
        if self.example:
            d["example"] = self.example
        return d


@dataclass(frozen=True)
class ListFieldDefinition:
    """A list/table field with sub-fields."""

    name: str
    description: str = ""
    fields: tuple[FieldDefinition, ...] = ()

    def to_prompt_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "name": self.name,
            "type": "list",
        }
        if self.description:
            d["description"] = self.description
        d["fields"] = [f.to_prompt_dict() for f in self.fields]
        return d


@dataclass(frozen=True)
class ExtractionResult:
    """Parsed result from LLM extraction response."""

    data: Dict[str, Any]
    selectors: Dict[str, Any]
    confidence: float
    raw_response: Dict[str, Any]


# ── Prompt Builder ────────────────────────────────────────────────────────────


class ExtractionPromptBuilder:
    """Builds extraction prompts and parses LLM responses."""

    def __init__(self, system_prompt: Optional[str] = None):
        self.system_prompt = system_prompt or SYSTEM_PROMPT

    def build_extraction_prompt(
        self,
        simplified_html: str,
        fields: List[FieldDefinition | ListFieldDefinition],
        *,
        page_context: Optional[str] = None,
    ) -> str:
        """Build the extraction prompt.

        Args:
            simplified_html: DOM from dom_simplifier.simplify_html().
            fields: Fields to extract.
            page_context: Optional description of the page (e.g. "utility bill dashboard").

        Returns:
            The user prompt string ready to send to an LLM.
        """
        field_specs = [f.to_prompt_dict() for f in fields]
        output_schema = self._build_output_schema(fields)

        parts = ["## Task\nExtract the following fields from the HTML below.\n"]

        if page_context:
            parts.append(f"## Page Context\n{page_context}\n")

        parts.append("## Fields to Extract")
        parts.append("```json")
        parts.append(json.dumps(field_specs, indent=2))
        parts.append("```\n")

        parts.append("## Expected Output Schema")
        parts.append("Return JSON matching this exact structure:")
        parts.append("```json")
        parts.append(json.dumps(output_schema, indent=2))
        parts.append("```\n")

        parts.append("## HTML")
        parts.append("```html")
        parts.append(simplified_html)
        parts.append("```")

        return "\n".join(parts)

    def build_selector_verification_prompt(
        self,
        simplified_html: str,
        selectors: Dict[str, str],
        expected_values: Dict[str, Any],
    ) -> str:
        """Build a prompt to verify that selectors still return expected values.

        Used for cache validation after site changes.
        """
        parts = [
            "## Task\nVerify that the CSS selectors below still extract the expected values from the HTML.\n",
            "## Selectors and Expected Values",
            "```json",
            json.dumps(
                {
                    name: {"selector": sel, "expected": expected_values.get(name)}
                    for name, sel in selectors.items()
                },
                indent=2,
            ),
            "```\n",
            '## Expected Output\nReturn JSON: `{"verified": true/false, "mismatches": [{"field": "name", "expected": "...", "actual": "..."}]}`\n',
            "## HTML",
            "```html",
            simplified_html,
            "```",
        ]
        return "\n".join(parts)

    def parse_response(self, response_data: Any) -> ExtractionResult:
        """Parse the LLM's JSON response into an ExtractionResult.

        Accepts either a dict (already parsed) or an LLMResponse object.
        """
        if hasattr(response_data, "parse_json"):
            data = response_data.parse_json()
        elif isinstance(response_data, str):
            data = json.loads(response_data)
        else:
            data = response_data

        if not isinstance(data, dict):
            raise ValueError(f"Expected dict from LLM, got {type(data).__name__}")

        extracted = data.get("data", {})
        selectors = data.get("selectors", {})
        confidence = data.get("confidence", 0.0)

        # Ensure confidence is a valid number in [0, 1]
        try:
            confidence = float(confidence)
            confidence = max(0.0, min(1.0, confidence))
        except (TypeError, ValueError):
            confidence = 0.0

        return ExtractionResult(
            data=extracted,
            selectors=selectors,
            confidence=confidence,
            raw_response=data,
        )

    def _build_output_schema(
        self, fields: List[FieldDefinition | ListFieldDefinition]
    ) -> Dict[str, Any]:
        """Build the expected JSON output schema for the LLM."""
        data_schema: Dict[str, Any] = {}
        selector_schema: Dict[str, Any] = {}

        for f in fields:
            if isinstance(f, ListFieldDefinition):
                # List fields have array values and a row selector
                row_schema = {}
                row_selectors = {}
                for sub in f.fields:
                    row_schema[sub.name] = f"<{sub.type}>"
                    row_selectors[sub.name] = "<css_selector>"
                data_schema[f.name] = [row_schema]
                selector_schema[f.name] = {
                    "row": "<css_selector_for_each_row>",
                    "fields": row_selectors,
                }
            else:
                type_hint = f"<{f.type}>"
                if f.type == "currency":
                    type_hint = "<number>"
                elif f.type == "date":
                    type_hint = "<ISO_date_string>"
                elif f.type == "boolean":
                    type_hint = "<true/false>"
                data_schema[f.name] = type_hint
                selector_schema[f.name] = "<css_selector>"

        return {
            "data": data_schema,
            "selectors": selector_schema,
            "confidence": "<float 0.0-1.0>",
        }


# ── Helpers ───────────────────────────────────────────────────────────────────


def fields_from_blueprint_extract(
    extract_config: Dict[str, Any],
) -> List[FieldDefinition | ListFieldDefinition]:
    """Convert a blueprint V3 extract config to FieldDefinition list.

    Accepts the 'fields' dict from a blueprint's extract section:
    ```
    {
      "account_number": {"type": "text", "description": "The account number"},
      "usage_history": {
        "type": "list",
        "description": "Monthly usage",
        "fields": {
          "month": {"type": "text"},
          "cost": {"type": "currency"}
        }
      }
    }
    ```
    """
    result: List[FieldDefinition | ListFieldDefinition] = []

    for name, spec in extract_config.items():
        if not isinstance(spec, dict):
            continue

        field_type = spec.get("type", "text")

        if field_type in ("list", "table"):
            sub_fields = []
            for sub_name, sub_spec in spec.get("fields", {}).items():
                if isinstance(sub_spec, dict):
                    sub_fields.append(
                        FieldDefinition(
                            name=sub_name,
                            type=sub_spec.get("type", "text"),
                            description=sub_spec.get("description", ""),
                            sensitive=sub_spec.get("sensitive", False),
                            example=sub_spec.get("example"),
                        )
                    )
            result.append(
                ListFieldDefinition(
                    name=name,
                    description=spec.get("description", ""),
                    fields=tuple(sub_fields),
                )
            )
        else:
            result.append(
                FieldDefinition(
                    name=name,
                    type=field_type,
                    description=spec.get("description", ""),
                    sensitive=spec.get("sensitive", False),
                    example=spec.get("example"),
                )
            )

    return result
