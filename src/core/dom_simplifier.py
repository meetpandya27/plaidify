"""
DOM Simplifier — produces a token-efficient DOM representation for LLM extraction.

Takes raw HTML from a Playwright page and produces a cleaned, compact version
suitable for sending to an LLM. Strips non-essential elements (scripts, styles,
SVGs, etc.), collapses whitespace, assigns stable element IDs (data-pid),
and estimates token count.

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
        "data-pid",
    }
)

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

# Default token budget — warn if exceeded
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


# ── HTML Cleaner (Parser-based) ───────────────────────────────────────────────


class _DOMCleaner(HTMLParser):
    """
    Streaming HTML parser that strips unwanted elements and assigns data-pid
    attributes to all surviving elements.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._output = StringIO()
        self._element_map: Dict[str, ElementInfo] = {}
        self._pid_counter = 0
        self._skip_depth = 0  # > 0 means we're inside a stripped tag
        self._tag_stack: List[str] = []  # stack of open tags for nesting

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

        # Assign a data-pid
        pid = self._next_pid()
        attr_dict = {}
        for name, value in attrs:
            name_lower = name.lower()
            if name_lower in KEEP_ATTRS and value is not None:
                attr_dict[name_lower] = value

        attr_dict["data-pid"] = pid

        # Build the element info
        info = ElementInfo(tag=tag, pid=pid, attrs={k: v for k, v in attr_dict.items() if k != "data-pid"})
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


def estimate_tokens(text: str) -> int:
    """Estimate the number of LLM tokens in a string (GPT-family approximation)."""
    return max(1, len(text) // CHARS_PER_TOKEN)


def simplify_html(raw_html: str, token_budget: int = DEFAULT_TOKEN_BUDGET) -> SimplifiedDOM:
    """
    Simplify raw HTML into a token-efficient representation.

    Args:
        raw_html: The full HTML string from a page.
        token_budget: Maximum recommended tokens. A warning is logged if exceeded.

    Returns:
        SimplifiedDOM with cleaned HTML, element map, and token estimate.
    """
    original_length = len(raw_html)

    cleaner = _DOMCleaner()
    cleaner.feed(raw_html)
    cleaned_html, element_map = cleaner.get_result()

    simplified_length = len(cleaned_html)
    token_est = estimate_tokens(cleaned_html)
    reduction = (1 - simplified_length / original_length) * 100 if original_length > 0 else 0
    over_budget = token_est > token_budget

    if over_budget:
        logger.warning(
            f"Simplified DOM exceeds token budget: {token_est} tokens (budget: {token_budget})",
            extra={"extra_data": {"tokens": token_est, "budget": token_budget}},
        )

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
