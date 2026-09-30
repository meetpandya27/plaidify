"""A logged value can't forge a record: both formats keep each record on one line."""

import json
import logging

from src.logging_config import JSONFormatter, TextFormatter

FORGED = "hydro_one\n2026-09-30 12:00:00 ERROR    [src.audit] admin password changed\r"


def _record(message: str, *args) -> logging.LogRecord:
    return logging.LogRecord("src.engine", logging.INFO, __file__, 1, message, args, None)


def test_text_format_escapes_line_breaks_in_logged_values():
    line = TextFormatter().format(_record("Initiating connection to %s", FORGED))
    assert "\n" not in line and "\r" not in line
    assert "hydro_one\\n2026-09-30" in line and line.endswith("changed\\r")


def test_text_format_keeps_tracebacks_multiline():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = logging.LogRecord("src.engine", logging.ERROR, __file__, 1, "failed", (), sys.exc_info())
    formatted = TextFormatter().format(record)
    assert "failed\nTraceback (most recent call last):" in formatted


def test_json_format_is_one_line_with_the_value_intact():
    line = JSONFormatter().format(_record("Initiating connection to %s", FORGED))
    assert "\n" not in line and "\r" not in line
    assert json.loads(line)["message"] == f"Initiating connection to {FORGED}"
