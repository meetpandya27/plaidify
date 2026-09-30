"""
DOM Simplifier — produces a token-efficient DOM representation for LLM extraction.

Takes raw HTML from a Playwright page and produces a cleaned, compact version
suitable for sending to an LLM. Strips non-essential elements (scripts, styles,
SVGs, etc.), collapses whitespace, and estimates token count.

What leaves the process is kept to what the model needs: hidden form fields
(anti-forgery and session tokens, account ids) are dropped, password values
are never included, secret-looking URL parameters are redacted, and a page over
the token budget is trimmed (long tables and lists first, then truncation)
instead of being sent whole.

Element ids (``p1``, ``p2`` …) are kept only in ``element_map``; they are not
written into the HTML, because a selector built on them would match nothing on
the live page.

Usage:
    simplifier = DOMSimplifier()
    result = await simplifier.simplify(page)
    # result.html — cleaned HTML string
    # result.element_map — {pid: {tag, text, attrs}} for quick lookup
    # result.token_estimate — approximate token count
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from io import StringIO
from typing import Dict, List, Optional

from src.logging_config import get_logger

logger = get_logger("dom_simplifier")

# ── Constants ─────────────────────────────────────────────────────────────────

# Elements to strip entirely (including their children)
STRIP_TAGS = frozenset(
    {
        "script",
        "style",
        "svg",
        "noscript",
        "iframe",
        "link",
        "meta",
        "head",
        "template",
        "object",
        "embed",
        "applet",
    }
)

# Elements that are invisible / structural noise
SKIP_TAGS = frozenset(
    {
        "br",
        "hr",
        "wbr",
        "col",
        "colgroup",
        "source",
        "track",
        "param",
    }
)

# Self-closing tags (no children)
VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)

# Attributes to keep (all others are dropped to save tokens)
KEEP_ATTRS = frozenset(
    {
        "id",
        "class",
        "name",
        "type",
        "value",
        "placeholder",
        "href",
        "src",
        "alt",
        "title",
        "role",
        "aria-label",
        "aria-labelledby",
        "aria-describedby",
        "for",
        "action",
        "method",
    }
)

# Attributes holding URLs, whose secret-looking query parameters are redacted.
URL_ATTRS = frozenset({"href", "src", "action"})
_SECRET_PARAM = re.compile(
    r"(?i)(^|[?&;])([\w.\-]*?(?:token|session|sessid|sid|csrf|xsrf|auth|key|secret|code|state|nonce|jwt|sig|signature|password|pwd|otp|ticket|saml)[\w.\-]*)=([^&#;]*)"
)
# Row caps tried, in order, when a page is over the token budget.
_ROW_CAPS = (50, 20, 5)
_ROW_CONTAINERS = {"tr": frozenset({"table", "thead", "tbody", "tfoot"}), "li": frozenset({"ul", "ol"})}
_TRUNCATION_NOTE = "\n[... page truncated to fit the token budget ...]"

# Tags that carry interactive or semantic meaning (always keep)
IMPORTANT_TAGS = frozenset(
    {
        "a",
        "button",
        "input",
        "select",
        "textarea",
        "form",
        "label",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "table",
        "thead",
        "tbody",
        "tr",
        "th",
        "td",
        "ul",
        "ol",
        "li",
        "nav",
        "main",
        "article",
        "section",
        "header",
        "footer",
        "img",
        "p",
        "span",
        "div",
    }
)

# Approximate chars per token (for GPT-family models)
CHARS_PER_TOKEN = 4

# Default token budget — larger pages are trimmed to fit
DEFAULT_TOKEN_BUDGET = 30_000


# ── Data Classes ──────────────────────────────────────────────────────────────


@dataclass
class ElementInfo:
    """Info about a single DOM element, stored in the element map."""

    tag: str
    pid: str
    text: str = ""
    attrs: Dict[str, str] = field(default_factory=dict)
    children_pids: List[str] = field(default_factory=list)


@dataclass
class SimplifiedDOM:
    """Result of DOM simplification."""

    html: str
    element_map: Dict[str, ElementInfo]
    token_estimate: int
    original_length: int
    simplified_length: int
    reduction_pct: float
    over_budget: bool
    truncated: bool = False


# ── HTML Cleaner (Parser-based) ───────────────────────────────────────────────


class _DOMCleaner(HTMLParser):
    """
    Streaming HTML parser that strips unwanted elements and records every
    surviving element in an element map under an internal id.
    """

    def __init__(self, max_rows: Optional[int] = None) -> None:
        super().__init__(convert_charrefs=True)
        self._output = StringIO()
        self._element_map: Dict[str, ElementInfo] = {}
        self._pid_counter = 0
        self._skip_depth = 0  # > 0 means we're inside a stripped tag
        self._tag_stack: List[str] = []  # stack of open tags for nesting
        self._max_rows = max_rows
        self._row_counts: Dict[str, int] = {}
        self.rows_dropped = 0

    def _next_pid(self) -> str:
        self._pid_counter += 1
        return f"p{self._pid_counter}"

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()

        # If already inside a stripped subtree, just track depth
        if self._skip_depth > 0:
            if tag not in VOID_TAGS:
                self._skip_depth += 1
            return

        # Strip entire subtree for blacklisted tags
        if tag in STRIP_TAGS:
            if tag not in VOID_TAGS:
                self._skip_depth += 1
            return

        # Skip noise tags but don't descend
        if tag in SKIP_TAGS:
            return

        raw_attrs = {name.lower(): value for name, value in attrs}
        input_type = (raw_attrs.get("type") or "").strip().lower()

        # Hidden inputs carry anti-forgery and session tokens, not page data;
        # elements with the hidden attribute are not rendered at all.
        if (tag == "input" and input_type == "hidden") or "hidden" in raw_attrs:
            if tag not in VOID_TAGS:
                self._skip_depth += 1
            return

        # Keep long tables and lists to their first rows when trimming for budget.
        if self._max_rows is not None and tag in _ROW_CONTAINERS and self._tag_stack:
            parent_pid = self._tag_stack[-1]
            parent = self._element_map.get(parent_pid)
            if parent is not None and parent.tag in _ROW_CONTAINERS[tag]:
                count = self._row_counts.get(parent_pid, 0) + 1
                self._row_counts[parent_pid] = count
                if count > self._max_rows:
                    self.rows_dropped += 1
                    self._skip_depth += 1
                    return

        pid = self._next_pid()
        attr_dict = {}
        for name_lower, value in raw_attrs.items():
            if name_lower not in KEEP_ATTRS or value is None:
                continue
            if name_lower == "value" and (input_type == "password" or tag == "textarea"):
                continue
            if name_lower in URL_ATTRS:
                value = _redact_url(value)
            attr_dict[name_lower] = value

        # Build the element info
        info = ElementInfo(tag=tag, pid=pid, attrs=dict(attr_dict))
        self._element_map[pid] = info

        # Wire parent-child
        if self._tag_stack:
            parent_pid = self._tag_stack[-1]
            if parent_pid in self._element_map:
                self._element_map[parent_pid].children_pids.append(pid)

        # Write opening tag
        attr_str = " ".join(f'{k}="{_escape_attr(v)}"' for k, v in attr_dict.items())
        self._output.write(f"<{tag} {attr_str}>" if attr_str else f"<{tag}>")

        if tag in VOID_TAGS:
            return  # no closing tag, don't push stack
        self._tag_stack.append(pid)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()

        if self._skip_depth > 0:
            if tag not in VOID_TAGS:
                self._skip_depth -= 1
            return

        if tag in STRIP_TAGS or tag in SKIP_TAGS or tag in VOID_TAGS:
            return

        if self._tag_stack:
            self._tag_stack.pop()

        self._output.write(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return

        # Collapse whitespace
        text = _collapse_whitespace(data)
        if not text:
            return

        self._output.write(text)

        # Attach text to nearest parent element
        if self._tag_stack:
            pid = self._tag_stack[-1]
            if pid in self._element_map:
                existing = self._element_map[pid].text
                self._element_map[pid].text = (existing + " " + text).strip() if existing else text

    def handle_comment(self, data: str) -> None:
        # Strip all HTML comments
        pass

    def get_result(self) -> tuple[str, Dict[str, ElementInfo]]:
        return self._output.getvalue(), self._element_map


# ── Helpers ───────────────────────────────────────────────────────────────────


def _collapse_whitespace(text: str) -> str:
    """Collapse runs of whitespace into a single space, strip edges."""
    return re.sub(r"\s+", " ", text).strip()


def _escape_attr(value: str) -> str:
    """Minimal HTML attribute escaping."""
    return value.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def _redact_url(value: str) -> str:
    """Blank the values of secret-looking query / path parameters (token=, jsessionid=, ...)."""
    return _SECRET_PARAM.sub(lambda m: f"{m.group(1)}{m.group(2)}=REDACTED", value)


def _clean(raw_html: str, max_rows: Optional[int] = None) -> tuple[str, Dict[str, ElementInfo], int]:
    cleaner = _DOMCleaner(max_rows=max_rows)
    cleaner.feed(raw_html)
    cleaner.close()
    html, element_map = cleaner.get_result()
    return html, element_map, cleaner.rows_dropped


def _truncate_html(html: str, max_chars: int) -> str:
    """Cut ``html`` to ``max_chars``, at a tag boundary, with a visible note."""
    budget = max(0, max_chars - len(_TRUNCATION_NOTE))
    if len(html) <= max_chars:
        return html
    cut = html.rfind(">", 0, budget)
    return html[: cut + 1 if cut > 0 else budget] + _TRUNCATION_NOTE


def estimate_tokens(text: str) -> int:
    """Estimate the number of LLM tokens in a string (GPT-family approximation)."""
    return max(1, len(text) // CHARS_PER_TOKEN)


def simplify_html(raw_html: str, token_budget: int = DEFAULT_TOKEN_BUDGET) -> SimplifiedDOM:
    """
    Simplify raw HTML into a token-efficient representation.

    Args:
        raw_html: The full HTML string from a page.
        token_budget: Maximum tokens to produce. A larger page keeps only the
            first rows of long tables and lists and, if still too large, is
            truncated.

    Returns:
        SimplifiedDOM with cleaned HTML, element map, and token estimate.
    """
    original_length = len(raw_html)

    cleaned_html, element_map, _dropped = _clean(raw_html)
    over_budget = estimate_tokens(cleaned_html) > token_budget
    truncated = False

    if over_budget:
        full_tokens = estimate_tokens(cleaned_html)
        rows_dropped = 0
        for max_rows in _ROW_CAPS:
            cleaned_html, element_map, rows_dropped = _clean(raw_html, max_rows=max_rows)
            if estimate_tokens(cleaned_html) <= token_budget:
                break
        if estimate_tokens(cleaned_html) > token_budget:
            cleaned_html = _truncate_html(cleaned_html, token_budget * CHARS_PER_TOKEN)
        truncated = True
        logger.warning(
            f"Simplified DOM exceeded the token budget: {full_tokens} tokens (budget: {token_budget}); trimmed",
            extra={"extra_data": {"tokens": full_tokens, "budget": token_budget, "rows_dropped": rows_dropped}},
        )

    simplified_length = len(cleaned_html)
    token_est = estimate_tokens(cleaned_html)
    reduction = (1 - simplified_length / original_length) * 100 if original_length > 0 else 0

    logger.info(
        f"DOM simplified: {original_length} → {simplified_length} chars "
        f"({reduction:.1f}% reduction), ~{token_est} tokens, "
        f"{len(element_map)} elements",
    )

    return SimplifiedDOM(
        html=cleaned_html,
        element_map=element_map,
        token_estimate=token_est,
        original_length=original_length,
        simplified_length=simplified_length,
        reduction_pct=round(reduction, 1),
        over_budget=over_budget,
        truncated=truncated,
    )


# ── Playwright Integration ────────────────────────────────────────────────────


class DOMSimplifier:
    """
    High-level DOM simplifier that works with Playwright pages.

    Usage:
        simplifier = DOMSimplifier()
        result = await simplifier.simplify(page)
    """

    def __init__(self, token_budget: int = DEFAULT_TOKEN_BUDGET) -> None:
        self.token_budget = token_budget

    async def simplify(self, page) -> SimplifiedDOM:
        """
        Get the current page's HTML and simplify it.

        Args:
            page: A Playwright Page object.

        Returns:
            SimplifiedDOM result.
        """
        raw_html = await page.content()
        return simplify_html(raw_html, token_budget=self.token_budget)

    async def get_visible_text(self, page) -> str:
        """
        Extract only the visible text from a page (for fallback prompts).

        Args:
            page: A Playwright Page object.

        Returns:
            Plain text string of visible content.
        """
        return await page.evaluate("""
            () => {
                const walker = document.createTreeWalker(
                    document.body,
                    NodeFilter.SHOW_TEXT,
                    {
                        acceptNode: (node) => {
                            const el = node.parentElement;
                            if (!el) return NodeFilter.FILTER_REJECT;
                            const style = window.getComputedStyle(el);
                            if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
                                return NodeFilter.FILTER_REJECT;
                            }
                            const tag = el.tagName.toLowerCase();
                            if (['script', 'style', 'noscript', 'svg'].includes(tag)) {
                                return NodeFilter.FILTER_REJECT;
                            }
                            return NodeFilter.FILTER_ACCEPT;
                        }
                    }
                );
                const parts = [];
                let node;
                while (node = walker.nextNode()) {
                    const text = node.textContent.trim();
                    if (text) parts.push(text);
                }
                return parts.join(' ');
            }
        """)
