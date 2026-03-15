"""Tests for the Plaidify CLI."""

import json
import os
import tempfile

from click.testing import CliRunner

from plaidify.cli import cli


runner = CliRunner()


class TestCLIVersion:
    def test_version(self):
        result = runner.invoke(cli, ["--version"])
        assert result.exit_code == 0
        assert "plaidify" in result.output.lower()
        assert "0.3.0" in result.output

    def test_help(self):
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "Plaidify" in result.output
        assert "connect" in result.output
        assert "blueprint" in result.output
        assert "serve" in result.output
        assert "demo" in result.output


class TestBlueprintValidate:
    def test_validate_valid_blueprint(self):
        bp = {
            "schema_version": "2",
            "name": "Test Site",
            "domain": "test.com",
            "auth": [
                {"action": "goto", "url": "https://test.com/login"},
                {"action": "fill", "selector": "#user", "value": "{{username}}"},
                {"action": "click", "selector": "#login"},
            ],
            "extract": {
                "balance": {"type": "currency", "selector": "#balance"},
                "name": {"type": "text", "selector": "#name"},
            },
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(bp, f)
            f.flush()
            try:
                result = runner.invoke(cli, ["blueprint", "validate", f.name])
                assert result.exit_code == 0
                assert "valid" in result.output.lower()
                assert "Test Site" in result.output
            finally:
                os.unlink(f.name)

    def test_validate_missing_name(self):
        bp = {
            "schema_version": "2",
            "domain": "test.com",
            "auth": [{"action": "goto", "url": "https://test.com"}],
            "extract": {"x": {"type": "text", "selector": "#x"}},
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(bp, f)
            f.flush()
            try:
                result = runner.invoke(cli, ["blueprint", "validate", f.name])
                assert result.exit_code != 0
                assert "name" in result.output.lower()
            finally:
                os.unlink(f.name)

    def test_validate_missing_auth(self):
        bp = {
            "name": "Test",
            "domain": "test.com",
            "extract": {"x": {"type": "text", "selector": "#x"}},
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(bp, f)
            f.flush()
            try:
                result = runner.invoke(cli, ["blueprint", "validate", f.name])
                assert result.exit_code != 0
                assert "auth" in result.output.lower()
            finally:
                os.unlink(f.name)

    def test_validate_missing_extract(self):
        bp = {
            "name": "Test",
            "domain": "test.com",
            "auth": [{"action": "goto", "url": "https://test.com"}],
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(bp, f)
            f.flush()
            try:
                result = runner.invoke(cli, ["blueprint", "validate", f.name])
                assert result.exit_code != 0
                assert "extract" in result.output.lower()
            finally:
                os.unlink(f.name)

    def test_validate_unknown_action(self):
        bp = {
            "name": "Test",
            "domain": "test.com",
            "auth": [{"action": "teleport", "url": "https://test.com"}],
            "extract": {"x": {"type": "text", "selector": "#x"}},
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(bp, f)
            f.flush()
            try:
                result = runner.invoke(cli, ["blueprint", "validate", f.name])
                assert result.exit_code != 0
                assert "teleport" in result.output
            finally:
                os.unlink(f.name)

    def test_validate_unknown_field_type(self):
        bp = {
            "name": "Test",
            "domain": "test.com",
            "auth": [{"action": "goto", "url": "https://test.com"}],
            "extract": {"x": {"type": "alien_data", "selector": "#x"}},
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(bp, f)
            f.flush()
            try:
                result = runner.invoke(cli, ["blueprint", "validate", f.name])
                assert result.exit_code != 0
                assert "alien_data" in result.output
            finally:
                os.unlink(f.name)

    def test_validate_missing_selector(self):
        bp = {
            "name": "Test",
            "domain": "test.com",
            "auth": [{"action": "goto", "url": "https://test.com"}],
            "extract": {"x": {"type": "text"}},
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(bp, f)
            f.flush()
            try:
                result = runner.invoke(cli, ["blueprint", "validate", f.name])
                assert result.exit_code != 0
                assert "selector" in result.output.lower()
            finally:
                os.unlink(f.name)

    def test_validate_invalid_json(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("{invalid json")
            f.flush()
            try:
                result = runner.invoke(cli, ["blueprint", "validate", f.name])
                assert result.exit_code != 0
                assert "json" in result.output.lower()
            finally:
                os.unlink(f.name)

    def test_validate_v1_schema(self):
        bp = {
            "version": "1",
            "name": "Old Site",
            "domain": "old.com",
            "steps": [{"action": "goto", "url": "https://old.com"}],
            "extract": {"x": {"type": "text", "selector": "#x"}},
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(bp, f)
            f.flush()
            try:
                result = runner.invoke(cli, ["blueprint", "validate", f.name])
                assert result.exit_code == 0
                assert "valid" in result.output.lower()
            finally:
                os.unlink(f.name)

    def test_validate_nonexistent_file(self):
        result = runner.invoke(cli, ["blueprint", "validate", "/nonexistent/file.json"])
        assert result.exit_code != 0


class TestBlueprintSubcommands:
    def test_blueprint_help(self):
        result = runner.invoke(cli, ["blueprint", "--help"])
        assert result.exit_code == 0
        assert "validate" in result.output
        assert "list" in result.output
        assert "info" in result.output
        assert "test" in result.output

    def test_connect_help(self):
        result = runner.invoke(cli, ["connect", "--help"])
        assert result.exit_code == 0
        assert "--username" in result.output
        assert "--password" in result.output
