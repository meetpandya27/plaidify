"""
Tests for Issue #1: DOM simplification module for LLM extraction.

Covers:
- Stripping script, style, svg, noscript, iframe, comments
- Whitespace collapsing
- data-pid assignment to surviving elements
- Token estimation
- Element map structure
- Reduction percentage (>30% from complex pages)
- Token budget warning
- Playwright integration (DOMSimplifier class)
"""

from unittest.mock import AsyncMock

import pytest

from src.core.dom_simplifier import (
    DEFAULT_TOKEN_BUDGET,
    STRIP_TAGS,
    DOMSimplifier,
    ElementInfo,
    SimplifiedDOM,
    _collapse_whitespace,
    estimate_tokens,
    simplify_html,
)

# ── Fixtures: Sample DOM strings ──────────────────────────────────────────────

SIMPLE_HTML = """
<html>
<head><title>Test</title><style>.foo { color: red; }</style></head>
<body>
  <h1>Hello World</h1>
  <p class="intro">Welcome to the site.</p>
</body>
</html>
"""

COMPLEX_HTML = """
<html>
<head>
  <title>My Bank Dashboard</title>
  <meta charset="utf-8"/>
  <link rel="stylesheet" href="styles.css"/>
  <style>
    body { font-family: Arial; }
    .dashboard { display: grid; }
    .account-number { font-weight: bold; }
    @media (max-width: 768px) { .sidebar { display: none; } }
  </style>
  <script>
    window.analytics = { track: function() {} };
    console.log("page loaded");
    (function() { var x = document.createElement('script'); x.src='tracker.js'; })();
  </script>
  <script src="https://cdn.example.com/bundle.min.js"></script>
  <noscript><p>JavaScript is required.</p></noscript>
</head>
<body>
  <!-- Navigation -->
  <nav id="main-nav" class="navigation">
    <a href="/dashboard">Dashboard</a>
    <a href="/accounts">Accounts</a>
    <a href="/payments">Payments</a>
  </nav>

  <!-- SVG icon sprite — usually huge -->
  <svg xmlns="http://www.w3.org/2000/svg" style="display:none">
    <symbol id="icon-home"><path d="M10 20v-6h4v6h5v-8h3L12 3 2 12h3v8z"/></symbol>
    <symbol id="icon-settings"><path d="M19.14,12.94c0.04-0.3,0.06-0.61,0.06-0.94"/></symbol>
  </svg>

  <main>
    <div class="account-header">
      <h1>Welcome, John Doe</h1>
      <span class="account-number">ACC-12345678</span>
    </div>

    <div class="balance-section">
      <p class="label">Current Balance</p>
      <p class="amount">$4,521.30</p>
    </div>

    <div class="due-date">
      <p>Next payment due: <strong>March 25, 2026</strong></p>
    </div>

    <table class="transactions">
      <thead>
        <tr><th>Date</th><th>Description</th><th>Amount</th></tr>
      </thead>
      <tbody>
        <tr><td>03/14</td><td>Electric Bill</td><td>-$142.57</td></tr>
        <tr><td>03/12</td><td>Payroll Deposit</td><td>+$3,200.00</td></tr>
        <tr><td>03/10</td><td>Grocery Store</td><td>-$67.42</td></tr>
      </tbody>
    </table>

    <form id="quick-pay" action="/pay" method="POST">
      <label for="pay-amount">Amount</label>
      <input type="text" id="pay-amount" name="amount" placeholder="$0.00"/>
      <button type="submit">Pay Now</button>
    </form>
  </main>

  <footer>
    <p>&copy; 2026 My Bank</p>
  </footer>

  <!-- More tracking scripts -->
  <script>window.gtag('config', 'UA-000000');</script>
  <script async src="https://www.googletagmanager.com/gtag.js"></script>

  <iframe src="https://ads.example.com/banner" width="728" height="90"></iframe>
</body>
</html>
"""

MINIMAL_HTML = "<div><p>Hello</p></div>"

EMPTY_HTML = ""

WHITESPACE_HTML = """
<div>
    <p>   Multiple     spaces   and
    newlines    everywhere   </p>
</div>
"""

NESTED_SCRIPT_HTML = """
<div>
  <p>Before script</p>
  <script>
    var x = "<div>fake html inside script</div>";
    // lots of JS code here
    function foo() { return 42; }
  </script>
  <p>After script</p>
</div>
"""

DEEP_NESTING_HTML = """
<div><div><div><div><div><div>
  <span>Deep content</span>
</div></div></div></div></div></div>
"""


# ── Tests: simplify_html ─────────────────────────────────────────────────────


class TestSimplifyHTML:
    """Test the core simplify_html function."""

    def test_returns_simplified_dom(self):
        result = simplify_html(SIMPLE_HTML)
        assert isinstance(result, SimplifiedDOM)
        assert isinstance(result.html, str)
        assert isinstance(result.element_map, dict)
        assert isinstance(result.token_estimate, int)

    def test_strips_script_tags(self):
        result = simplify_html(COMPLEX_HTML)
        assert "<script" not in result.html
        assert "analytics" not in result.html
        assert "gtag" not in result.html

    def test_strips_style_tags(self):
        result = simplify_html(COMPLEX_HTML)
        assert "<style" not in result.html
        assert "font-family" not in result.html

    def test_strips_svg_tags(self):
        result = simplify_html(COMPLEX_HTML)
        assert "<svg" not in result.html
        assert "<symbol" not in result.html
        assert "icon-home" not in result.html

    def test_strips_noscript_tags(self):
        result = simplify_html(COMPLEX_HTML)
        assert "<noscript" not in result.html
        assert "JavaScript is required" not in result.html

    def test_strips_iframe_tags(self):
        result = simplify_html(COMPLEX_HTML)
        assert "<iframe" not in result.html
        assert "ads.example.com" not in result.html

    def test_strips_html_comments(self):
        result = simplify_html(COMPLEX_HTML)
        assert "<!--" not in result.html
        assert "Navigation" not in result.html  # comment content

    def test_strips_head_tag(self):
        result = simplify_html(COMPLEX_HTML)
        assert "<head" not in result.html
        assert "<meta" not in result.html

    def test_preserves_visible_content(self):
        result = simplify_html(COMPLEX_HTML)
        assert "Welcome, John Doe" in result.html
        assert "ACC-12345678" in result.html
        assert "$4,521.30" in result.html
        assert "March 25, 2026" in result.html
        assert "Electric Bill" in result.html

    def test_preserves_interactive_elements(self):
        result = simplify_html(COMPLEX_HTML)
        assert "<form" in result.html
        assert "<input" in result.html
        assert "<button" in result.html
        assert "<a " in result.html

    def test_preserves_table_structure(self):
        result = simplify_html(COMPLEX_HTML)
        assert "<table" in result.html
        assert "<thead" in result.html
        assert "<tbody" in result.html
        assert "<tr" in result.html
        assert "<th" in result.html
        assert "<td" in result.html

    def test_assigns_data_pid_attributes(self):
        result = simplify_html(SIMPLE_HTML)
        assert 'data-pid="p' in result.html
        # Every element in the map should have a pid
        for pid, info in result.element_map.items():
            assert pid.startswith("p")
            assert info.pid == pid

    def test_pids_are_unique(self):
        result = simplify_html(COMPLEX_HTML)
        pids = list(result.element_map.keys())
        assert len(pids) == len(set(pids))

    def test_element_map_populated(self):
        result = simplify_html(COMPLEX_HTML)
        assert len(result.element_map) > 0
        # Check a known element exists with text
        found_balance = False
        for info in result.element_map.values():
            if "$4,521.30" in info.text:
                found_balance = True
                break
        assert found_balance

    def test_element_map_has_correct_structure(self):
        result = simplify_html(SIMPLE_HTML)
        for _pid, info in result.element_map.items():
            assert isinstance(info, ElementInfo)
            assert isinstance(info.tag, str)
            assert isinstance(info.pid, str)
            assert isinstance(info.attrs, dict)
            assert isinstance(info.children_pids, list)

    def test_collapses_whitespace(self):
        result = simplify_html(WHITESPACE_HTML)
        assert "Multiple     spaces" not in result.html
        assert "Multiple spaces and newlines everywhere" in result.html

    def test_significant_reduction_on_complex_page(self):
        """Complex pages should be reduced by at least 30%."""
        result = simplify_html(COMPLEX_HTML)
        assert result.reduction_pct > 30.0

    def test_reduction_stats_populated(self):
        result = simplify_html(COMPLEX_HTML)
        assert result.original_length > 0
        assert result.simplified_length > 0
        assert result.simplified_length < result.original_length

    def test_nested_script_content_stripped(self):
        result = simplify_html(NESTED_SCRIPT_HTML)
        assert "fake html inside script" not in result.html
        assert "function foo" not in result.html
        assert "Before script" in result.html
        assert "After script" in result.html

    def test_deep_nesting_preserved(self):
        result = simplify_html(DEEP_NESTING_HTML)
        assert "Deep content" in result.html

    def test_minimal_html(self):
        result = simplify_html(MINIMAL_HTML)
        assert "Hello" in result.html
        assert len(result.element_map) >= 2  # div + p

    def test_empty_html(self):
        result = simplify_html(EMPTY_HTML)
        assert result.html == ""
        assert result.token_estimate >= 0
        assert len(result.element_map) == 0


# ── Tests: Token Estimation ──────────────────────────────────────────────────


class TestTokenEstimation:
    """Test the estimate_tokens function."""

    def test_empty_string(self):
        assert estimate_tokens("") >= 0

    def test_short_string(self):
        est = estimate_tokens("Hello world")
        assert est >= 1
        assert est <= 10

    def test_proportional_to_length(self):
        short = estimate_tokens("Hello")
        long = estimate_tokens("Hello " * 100)
        assert long > short

    def test_complex_html_estimation(self):
        result = simplify_html(COMPLEX_HTML)
        assert result.token_estimate > 0
        # Simplified complex page should be under 5K tokens
        assert result.token_estimate < 5000


# ── Tests: Token Budget ──────────────────────────────────────────────────────


class TestTokenBudget:
    """Test token budget warnings."""

    def test_under_budget(self):
        result = simplify_html(SIMPLE_HTML, token_budget=100_000)
        assert not result.over_budget

    def test_over_budget_flagged(self):
        # Set absurdly low budget
        result = simplify_html(COMPLEX_HTML, token_budget=10)
        assert result.over_budget

    def test_default_budget(self):
        assert DEFAULT_TOKEN_BUDGET == 30_000


# ── Tests: Attribute Filtering ────────────────────────────────────────────────


class TestAttributeFiltering:
    """Test that only safe/useful attributes are kept."""

    def test_keeps_id_and_class(self):
        html = '<div id="main" class="container"><p>Text</p></div>'
        result = simplify_html(html)
        assert 'id="main"' in result.html
        assert 'class="container"' in result.html

    def test_keeps_form_attributes(self):
        html = '<form action="/submit" method="POST"><input type="text" name="email" placeholder="Enter email"/></form>'
        result = simplify_html(html)
        assert 'action="/submit"' in result.html
        assert 'method="POST"' in result.html
        assert 'type="text"' in result.html
        assert 'name="email"' in result.html
        assert 'placeholder="Enter email"' in result.html

    def test_keeps_aria_attributes(self):
        html = '<button aria-label="Close dialog" role="button">X</button>'
        result = simplify_html(html)
        assert 'aria-label="Close dialog"' in result.html
        assert 'role="button"' in result.html

    def test_drops_event_handlers(self):
        html = '<button onclick="alert(1)" onmouseover="track()">Click</button>'
        result = simplify_html(html)
        assert "onclick" not in result.html
        assert "onmouseover" not in result.html

    def test_drops_data_attributes_except_pid(self):
        html = '<div data-testid="foo" data-analytics="track">Content</div>'
        result = simplify_html(html)
        assert "data-testid" not in result.html
        assert "data-analytics" not in result.html
        assert "data-pid=" in result.html


# ── Tests: Helpers ────────────────────────────────────────────────────────────


class TestHelpers:
    """Test helper functions."""

    def test_collapse_whitespace(self):
        assert _collapse_whitespace("  hello   world  ") == "hello world"
        assert _collapse_whitespace("\n\t foo \n bar \t") == "foo bar"
        assert _collapse_whitespace("") == ""
        assert _collapse_whitespace("   ") == ""

    def test_all_strip_tags_handled(self):
        """Every tag in STRIP_TAGS is actually stripped."""
        from src.core.dom_simplifier import VOID_TAGS

        for tag in STRIP_TAGS:
            if tag == "head":
                # head needs to be inside html
                html = "<html><head><title>T</title></head><body><p>keep</p></body></html>"
            elif tag in VOID_TAGS or tag == "meta" or tag == "link":
                # Void tags can't contain children — just confirm the tag itself is stripped
                html = f'<div><{tag} class="remove-me"/><p>keep</p></div>'
                result = simplify_html(html)
                assert f"<{tag}" not in result.html, f"Void tag <{tag}> was not stripped"
                assert "keep" in result.html
                continue
            else:
                html = f"<div><{tag}>remove this content</{tag}><p>keep</p></div>"
            result = simplify_html(html)
            assert "remove this content" not in result.html, f"Tag <{tag}> was not stripped"
            assert "keep" in result.html


# ── Tests: DOMSimplifier (Playwright integration) ────────────────────────────


class TestDOMSimplifier:
    """Test the DOMSimplifier class with a mocked Playwright page."""

    @pytest.mark.asyncio
    async def test_simplify_calls_page_content(self):
        page = AsyncMock()
        page.content.return_value = SIMPLE_HTML

        simplifier = DOMSimplifier()
        result = await simplifier.simplify(page)

        page.content.assert_called_once()
        assert isinstance(result, SimplifiedDOM)
        assert "Hello World" in result.html

    @pytest.mark.asyncio
    async def test_simplify_with_custom_budget(self):
        page = AsyncMock()
        page.content.return_value = COMPLEX_HTML

        simplifier = DOMSimplifier(token_budget=50_000)
        result = await simplifier.simplify(page)
        assert not result.over_budget

    @pytest.mark.asyncio
    async def test_get_visible_text(self):
        page = AsyncMock()
        page.evaluate.return_value = "Welcome John Balance $4521.30"

        simplifier = DOMSimplifier()
        text = await simplifier.get_visible_text(page)

        page.evaluate.assert_called_once()
        assert "Welcome John" in text


# ── Tests: Edge Cases ─────────────────────────────────────────────────────────


class TestEdgeCases:
    """Test edge cases and malformed HTML."""

    def test_unclosed_tags(self):
        html = "<div><p>unclosed paragraph<p>second paragraph</div>"
        result = simplify_html(html)
        assert "unclosed paragraph" in result.html
        assert "second paragraph" in result.html

    def test_self_closing_tags(self):
        html = '<div><img src="photo.jpg" alt="Photo"/><br/><input type="text"/></div>'
        result = simplify_html(html)
        assert "<img" in result.html
        assert 'alt="Photo"' in result.html

    def test_entities_preserved(self):
        html = "<p>&amp; &lt; &gt; &copy; 2026</p>"
        result = simplify_html(html)
        assert "2026" in result.html

    def test_large_html_performance(self):
        """Simplification should handle large pages without error."""
        # Generate ~100KB of HTML
        rows = "\n".join(f"<tr><td>Row {i}</td><td>${i * 10}.00</td></tr>" for i in range(2000))
        html = f"<html><body><table><tbody>{rows}</tbody></table></body></html>"
        result = simplify_html(html)
        assert result.token_estimate > 0
        assert "Row 0" in result.html
        assert "Row 1999" in result.html

    def test_special_chars_in_attributes(self):
        html = '<a href="/search?q=hello&lang=en" title="Search &amp; Find">Link</a>'
        result = simplify_html(html)
        assert "Link" in result.html
        assert "href=" in result.html
