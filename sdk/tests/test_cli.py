"""Tests for the Plaidify CLI."""

import asyncio
import json
import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest
from click.testing import CliRunner

from plaidify import cli as cli_module
from plaidify.cli import cli
from plaidify.models import ConnectResult


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
        assert "rotate-key" in result.output


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
        assert "--password-stdin" in result.output


class _FakeClient:
    """Stands in for the SDK client and records what connect() received."""

    def __init__(self):
        self.calls = []

    async def connect(self, site, **kwargs):
        self.calls.append((site, kwargs))
        return ConnectResult(status="connected", data={"balance": 1})

    async def close(self):
        pass


class TestConnectSecrets:
    """The site password never travels on the command line."""

    def test_password_is_not_a_command_line_option(self):
        result = runner.invoke(cli, ["connect", "hydro_one", "-u", "alice", "-p", "hunter22"])
        assert result.exit_code != 0
        assert "No such option" in result.output

    def test_password_is_prompted_for_without_echo(self):
        fake = _FakeClient()
        with (
            mock.patch.object(cli_module, "_get_client", return_value=fake),
            mock.patch.object(cli_module.click, "prompt", return_value="hunter22") as prompt,
        ):
            result = runner.invoke(cli, ["connect", "hydro_one", "-u", "alice"])
        assert result.exit_code == 0, result.output
        assert prompt.call_args.kwargs.get("hide_input") is True
        assert fake.calls[0][1]["password"] == "hunter22"
        assert "hunter22" not in result.output

    def test_password_can_come_from_stdin(self):
        fake = _FakeClient()
        with mock.patch.object(cli_module, "_get_client", return_value=fake):
            result = runner.invoke(cli, ["connect", "hydro_one", "-u", "alice", "--password-stdin"], input="hunter22\n")
        assert result.exit_code == 0, result.output
        assert fake.calls[0][1]["password"] == "hunter22"

    def test_empty_stdin_is_an_error(self):
        with mock.patch.object(cli_module, "_get_client", return_value=_FakeClient()):
            result = runner.invoke(cli, ["connect", "hydro_one", "-u", "alice", "--password-stdin"], input="")
        assert result.exit_code != 0

    def test_blueprint_test_prompts_too(self):
        result = runner.invoke(cli, ["blueprint", "test", "--help"])
        assert "--password-stdin" in result.output
        assert "-p," not in result.output


class TestAsyncRunner:
    def test_runs_without_an_existing_event_loop(self):
        # asyncio.get_event_loop() raises here on Python 3.14; asyncio.run() does not.
        async def answer():
            return 42

        asyncio.set_event_loop(None)
        assert cli_module._run_async(answer()) == 42


class TestServe:
    def _checkout(self, tmp_path: Path) -> Path:
        root = tmp_path / "plaidify"
        (root / "src").mkdir(parents=True)
        (root / "src" / "main.py").write_text("app = None\n")
        # The SDK ships its own pyproject.toml; it must not pass for the server root.
        (root / "sdk").mkdir()
        (root / "sdk" / "pyproject.toml").write_text("[project]\nname = 'plaidify'\n")
        return root

    def test_serve_from_a_subdirectory_runs_in_the_checkout_root(self, tmp_path, monkeypatch):
        root = self._checkout(tmp_path)
        monkeypatch.chdir(root / "sdk")
        with mock.patch.object(cli_module.os, "execvp") as execvp, mock.patch.object(cli_module.os, "chdir") as chdir:
            result = runner.invoke(cli, ["serve", "--port", "9999"])
        assert result.exit_code == 0, result.output
        chdir.assert_called_once_with(root.resolve())
        args = execvp.call_args.args[1]
        assert "src.main:app" in args
        assert args[args.index("--port") + 1] == "9999"

    def test_serve_outside_a_checkout_fails_clearly(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        with mock.patch.object(cli_module.os, "execvp") as execvp:
            result = runner.invoke(cli, ["serve"])
        assert result.exit_code != 0
        assert "src/main.py" in result.output
        execvp.assert_not_called()


class TestAuditCommands:
    """/audit/verify and /audit/logs take a user's access token; they refuse API keys."""

    @staticmethod
    def _response(body):
        response = mock.Mock(status_code=200)
        response.json.return_value = body
        return response

    @pytest.mark.parametrize("command", ["verify", "logs"])
    def test_an_access_token_is_sent_as_a_bearer_token(self, command):
        body = {"total": 0, "valid": True, "errors": [], "entries": []}
        with mock.patch("httpx.get", return_value=self._response(body)) as get:
            result = runner.invoke(cli, ["--api-key", "jwt-abc", "audit", command])
        assert result.exit_code == 0, result.output
        assert get.call_args.kwargs["headers"] == {"Authorization": "Bearer jwt-abc"}

    @pytest.mark.parametrize("credential", [[], ["--api-key", "pk_abc"]])
    @pytest.mark.parametrize("command", ["verify", "logs"])
    def test_an_api_key_or_nothing_is_refused_before_any_request(self, command, credential):
        with mock.patch("httpx.get") as get:
            result = runner.invoke(cli, [*credential, "audit", command])
        assert result.exit_code == 1
        assert "not an API key" in result.output
        assert "plaidify login" in result.output
        get.assert_not_called()

    def test_verify_reports_every_error_including_the_chain_head(self):
        body = {
            "total": 12,
            "valid": False,
            "errors": [{"id": 4, "error": "entry hash mismatch"}, {"id": None, "error": "newest entries do not match"}],
            "error_count": 13,
        }
        with mock.patch("httpx.get", return_value=self._response(body)):
            result = runner.invoke(cli, ["--api-key", "jwt-abc", "audit", "verify"])
        assert result.exit_code == 1
        assert "13 error(s) in 12 entries" in result.output
        assert "Entry #4: entry hash mismatch" in result.output
        assert "Chain head: newest entries do not match" in result.output
        assert "... and 3 more" in result.output


class _FakeAuthClient:
    def __init__(self, fail=False):
        self.fail = fail
        self.logins = []
        self.closed = False

    async def login(self, username, password):
        self.logins.append((username, password))
        if self.fail:
            raise RuntimeError("Incorrect username or password.")
        from plaidify.models import AuthToken

        return AuthToken(access_token="jwt-token-123")

    async def close(self):
        self.closed = True


class TestLogin:
    def test_prints_only_the_token_on_stdout(self):
        fake = _FakeAuthClient()
        with mock.patch.object(cli_module, "_get_client", return_value=fake):
            result = runner.invoke(cli, ["login", "-u", "admin", "--password-stdin"], input="s3cret-pass\n")
        assert result.exit_code == 0, result.output
        assert result.stdout == "jwt-token-123\n"
        assert fake.logins == [("admin", "s3cret-pass")]
        assert fake.closed

    def test_the_prompt_is_hidden_and_goes_to_stderr(self):
        fake = _FakeAuthClient()
        with (
            mock.patch.object(cli_module, "_get_client", return_value=fake),
            mock.patch.object(cli_module.click, "prompt", return_value="s3cret-pass") as prompt,
        ):
            result = runner.invoke(cli, ["login", "-u", "admin"])
        assert result.exit_code == 0, result.output
        assert prompt.call_args.kwargs == {"hide_input": True, "err": True}
        assert result.stdout == "jwt-token-123\n"

    def test_a_refused_sign_in_prints_no_token(self):
        fake = _FakeAuthClient(fail=True)
        with mock.patch.object(cli_module, "_get_client", return_value=fake):
            result = runner.invoke(cli, ["login", "-u", "admin", "--password-stdin"], input="wrong\n")
        assert result.exit_code == 1
        assert result.stdout == ""
        assert "Incorrect username or password." in result.stderr
        assert "wrong" not in result.output
        assert fake.closed
