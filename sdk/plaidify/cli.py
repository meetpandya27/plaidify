"""
Plaidify CLI — command-line interface for Plaidify.

Usage:
    plaidify demo                         # Launch full demo (servers + browser)
    plaidify serve                        # Start the Plaidify API server
    plaidify connect <site> -u <user> -p <pass>
    plaidify blueprint list               # List available blueprints
    plaidify blueprint info <site>        # Show blueprint details
    plaidify blueprint validate <file>    # Validate a blueprint JSON file
    plaidify blueprint test <file>        # Test a blueprint against a live site
    plaidify health                       # Check server health
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from typing import Optional

import click

from plaidify import __version__


# ── Helpers ───────────────────────────────────────────────────────────────────


def _run_async(coro):
    """Run an async coroutine from sync Click context."""
    return asyncio.get_event_loop().run_until_complete(coro)


def _get_client(server_url: str, api_key: Optional[str] = None):
    """Create a Plaidify client."""
    from plaidify import Plaidify

    return Plaidify(server_url=server_url, api_key=api_key)


def _find_project_root() -> Path:
    """Walk up from CWD looking for a directory that has src/main.py or pyproject.toml."""
    cwd = Path.cwd()
    for d in [cwd, *cwd.parents]:
        if (d / "src" / "main.py").exists() or (d / "run_demo.py").exists():
            return d
    return cwd


def _echo_json(data: dict, pretty: bool = True):
    """Print JSON to stdout."""
    if pretty:
        click.echo(json.dumps(data, indent=2, default=str))
    else:
        click.echo(json.dumps(data, default=str))


def _echo_success(msg: str):
    click.secho(f"  ✓ {msg}", fg="green")


def _echo_error(msg: str):
    click.secho(f"  ✗ {msg}", fg="red", err=True)


def _echo_info(msg: str):
    click.secho(f"  → {msg}", fg="cyan")


def _echo_warn(msg: str):
    click.secho(f"  ⚠ {msg}", fg="yellow")


# ── Main Group ────────────────────────────────────────────────────────────────


@click.group()
@click.version_option(__version__, prog_name="plaidify")
@click.option("--server", "-s", envvar="PLAIDIFY_SERVER_URL", default="http://localhost:8000",
              help="Plaidify server URL.")
@click.option("--api-key", envvar="PLAIDIFY_API_KEY", default=None,
              help="JWT token or API key for authenticated endpoints.")
@click.pass_context
def cli(ctx: click.Context, server: str, api_key: Optional[str]):
    """Plaidify — The open-source API for authenticated web data."""
    ctx.ensure_object(dict)
    ctx.obj["server"] = server
    ctx.obj["api_key"] = api_key


# ── plaidify health ──────────────────────────────────────────────────────────


@cli.command()
@click.pass_context
def health(ctx: click.Context):
    """Check if the Plaidify server is running."""
    client = _get_client(ctx.obj["server"], ctx.obj["api_key"])

    async def _check():
        try:
            h = await client.health()
            _echo_success(f"Server is healthy (version {h.version})")
            if h.database:
                _echo_info(f"Database: {h.database}")
        except Exception as e:
            _echo_error(f"Server unreachable: {e}")
            sys.exit(1)
        finally:
            await client.close()

    _run_async(_check())


# ── plaidify connect ─────────────────────────────────────────────────────────


@cli.command()
@click.argument("site")
@click.option("-u", "--username", required=True, help="Username for the target site.")
@click.option("-p", "--password", required=True, help="Password for the target site.")
@click.option("--fields", default=None, help="Comma-separated list of fields to extract.")
@click.option("--json-output", "-j", is_flag=True, help="Output raw JSON instead of formatted.")
@click.pass_context
def connect(ctx: click.Context, site: str, username: str, password: str,
            fields: Optional[str], json_output: bool):
    """Connect to a site and extract data.

    Example:
        plaidify connect greengrid_energy -u demo_user -p demo_pass
    """
    client = _get_client(ctx.obj["server"], ctx.obj["api_key"])
    extract_fields = [f.strip() for f in fields.split(",")] if fields else None

    async def _connect():
        try:
            async def mfa_prompt(challenge):
                """Interactive MFA handler for CLI."""
                click.echo()
                _echo_warn(f"MFA required ({challenge.mfa_type})")
                if challenge.metadata:
                    _echo_info(f"Details: {challenge.metadata}")
                return click.prompt("  Enter MFA code")

            _echo_info(f"Connecting to {site}...")
            result = await client.connect(
                site,
                username=username,
                password=password,
                extract_fields=extract_fields,
                mfa_handler=mfa_prompt,
            )

            if result.connected:
                _echo_success("Connected successfully!")
                click.echo()
                if json_output:
                    _echo_json({"status": result.status, "data": result.data})
                else:
                    _echo_info(f"Status: {result.status}")
                    if result.data:
                        click.echo()
                        for key, value in result.data.items():
                            if isinstance(value, list):
                                click.echo(f"  {click.style(key, bold=True)}: ({len(value)} items)")
                                for i, item in enumerate(value[:5]):
                                    click.echo(f"    [{i}] {item}")
                                if len(value) > 5:
                                    click.echo(f"    ... and {len(value) - 5} more")
                            else:
                                click.echo(f"  {click.style(key, bold=True)}: {value}")
            else:
                _echo_warn(f"Status: {result.status}")
                if result.metadata:
                    _echo_info(f"Details: {result.metadata}")

        except Exception as e:
            _echo_error(str(e))
            sys.exit(1)
        finally:
            await client.close()

    _run_async(_connect())


# ── plaidify blueprint ───────────────────────────────────────────────────────


@cli.group()
def blueprint():
    """Manage and test blueprints."""
    pass


@blueprint.command("list")
@click.pass_context
def blueprint_list(ctx: click.Context):
    """List all available blueprints on the server."""
    client = _get_client(ctx.obj["server"], ctx.obj["api_key"])

    async def _list():
        try:
            result = await client.list_blueprints()
            if result.count == 0:
                _echo_warn("No blueprints found.")
                return
            click.echo()
            click.echo(f"  Available Blueprints ({result.count}):")
            click.echo(f"  {'─' * 60}")
            for bp in result.blueprints:
                mfa_badge = click.style(" [MFA]", fg="yellow") if bp.has_mfa else ""
                tags = click.style(f" ({', '.join(bp.tags)})", fg="bright_black") if bp.tags else ""
                click.echo(f"  {click.style(bp.site, bold=True, fg='green')}{mfa_badge}{tags}")
                click.echo(f"    {bp.name} — {bp.domain}")
            click.echo()
        except Exception as e:
            _echo_error(str(e))
            sys.exit(1)
        finally:
            await client.close()

    _run_async(_list())


@blueprint.command("info")
@click.argument("site")
@click.pass_context
def blueprint_info(ctx: click.Context, site: str):
    """Show detailed info about a blueprint."""
    client = _get_client(ctx.obj["server"], ctx.obj["api_key"])

    async def _info():
        try:
            bp = await client.get_blueprint(site)
            click.echo()
            click.echo(f"  {click.style(bp.name, bold=True)}")
            click.echo(f"  {'─' * 50}")
            click.echo(f"  Site:           {bp.site}")
            click.echo(f"  Domain:         {bp.domain}")
            click.echo(f"  Schema Version: {bp.schema_version}")
            click.echo(f"  MFA:            {'Yes' if bp.has_mfa else 'No'}")
            click.echo(f"  Tags:           {', '.join(bp.tags) if bp.tags else 'None'}")
            click.echo(f"  Extract Fields: {', '.join(bp.extract_fields) if bp.extract_fields else 'None'}")
            click.echo()
        except Exception as e:
            _echo_error(str(e))
            sys.exit(1)
        finally:
            await client.close()

    _run_async(_info())


@blueprint.command("validate")
@click.argument("filepath", type=click.Path(exists=True))
def blueprint_validate(filepath: str):
    """Validate a blueprint JSON file against the V2 schema.

    Example:
        plaidify blueprint validate ./connectors/my_site.json
    """
    path = Path(filepath)
    click.echo()
    _echo_info(f"Validating {path.name}...")

    try:
        with open(path) as f:
            raw = json.load(f)
    except json.JSONDecodeError as e:
        _echo_error(f"Invalid JSON: {e}")
        sys.exit(1)

    errors = []

    # Schema version
    version = raw.get("schema_version") or raw.get("version", "1")
    if version not in ("1", "2"):
        errors.append(f"Unknown schema_version: {version}")

    # Required top-level fields
    for field in ["name", "domain"]:
        if field not in raw:
            errors.append(f"Missing required field: '{field}'")

    # Auth steps
    auth = raw.get("auth") or raw.get("steps")
    if not auth:
        errors.append("Missing 'auth' section (authentication steps)")
    elif isinstance(auth, list):
        valid_actions = {
            "goto", "fill", "click", "wait", "screenshot", "extract",
            "conditional", "scroll", "select", "iframe", "wait_for_navigation",
            "execute_js",
        }
        for i, step in enumerate(auth):
            action = step.get("action")
            if not action:
                errors.append(f"Step {i}: missing 'action' field")
            elif action not in valid_actions:
                errors.append(f"Step {i}: unknown action '{action}'")

    # Extract
    extract = raw.get("extract")
    if not extract:
        errors.append("Missing 'extract' section (data extraction fields)")
    elif isinstance(extract, dict):
        valid_types = {
            "text", "currency", "date", "number", "email", "phone",
            "list", "table", "boolean", "sensitive",
        }
        for fname, fdef in extract.items():
            if isinstance(fdef, dict):
                ftype = fdef.get("type")
                if ftype and ftype not in valid_types:
                    errors.append(f"Field '{fname}': unknown type '{ftype}'")
                if "selector" not in fdef:
                    errors.append(f"Field '{fname}': missing 'selector'")

    # Report
    if errors:
        _echo_error(f"Validation failed with {len(errors)} error(s):")
        for err in errors:
            click.echo(f"    • {err}")
        click.echo()
        sys.exit(1)
    else:
        _echo_success(f"Blueprint '{path.name}' is valid (v{version})")
        # Summary
        field_count = len(extract) if isinstance(extract, dict) else 0
        step_count = len(auth) if isinstance(auth, list) else 0
        has_mfa = "mfa" in raw
        click.echo(f"    Name:   {raw.get('name', 'N/A')}")
        click.echo(f"    Domain: {raw.get('domain', 'N/A')}")
        click.echo(f"    Steps:  {step_count}")
        click.echo(f"    Fields: {field_count}")
        click.echo(f"    MFA:    {'Yes' if has_mfa else 'No'}")
        click.echo()


@blueprint.command("test")
@click.argument("filepath", type=click.Path(exists=True))
@click.option("-u", "--username", required=True, help="Username for the target site.")
@click.option("-p", "--password", required=True, help="Password for the target site.")
@click.option("--fields", default=None, help="Comma-separated list of fields to extract.")
@click.pass_context
def blueprint_test(ctx: click.Context, filepath: str, username: str, password: str,
                   fields: Optional[str]):
    """Test a blueprint against a live site.

    Runs the full connection flow and shows the extracted data.

    Example:
        plaidify blueprint test ./connectors/greengrid_energy.json -u demo_user -p demo_pass
    """
    path = Path(filepath)
    site = path.stem
    extract_fields = [f.strip() for f in fields.split(",")] if fields else None

    client = _get_client(ctx.obj["server"], ctx.obj["api_key"])

    async def _test():
        try:
            _echo_info(f"Testing blueprint: {site}")
            _echo_info(f"Connecting...")

            start = time.time()
            result = await client.connect(
                site,
                username=username,
                password=password,
                extract_fields=extract_fields,
            )
            elapsed = time.time() - start

            if result.connected:
                _echo_success(f"Connected in {elapsed:.1f}s")
                click.echo()
                if result.data:
                    field_count = len(result.data)
                    _echo_info(f"Extracted {field_count} field(s):")
                    for key, value in result.data.items():
                        if isinstance(value, list):
                            click.echo(f"    {click.style(key, bold=True)}: [{len(value)} items]")
                        elif isinstance(value, dict):
                            click.echo(f"    {click.style(key, bold=True)}: {{...}}")
                        else:
                            display = str(value)
                            if len(display) > 80:
                                display = display[:77] + "..."
                            click.echo(f"    {click.style(key, bold=True)}: {display}")
                else:
                    _echo_warn("No data extracted")
            else:
                _echo_warn(f"Status: {result.status} ({elapsed:.1f}s)")

            click.echo()
        except Exception as e:
            _echo_error(str(e))
            sys.exit(1)
        finally:
            await client.close()

    _run_async(_test())


# ── plaidify registry ────────────────────────────────────────────────────────


@cli.group()
def registry():
    """Browse and manage the blueprint registry."""
    pass


@registry.command("search")
@click.argument("query", required=False, default=None)
@click.option("--tag", "-t", default=None, help="Filter by tag.")
@click.option("--tier", default=None, help="Filter by quality tier (community/tested/certified).")
@click.pass_context
def registry_search(ctx: click.Context, query: Optional[str], tag: Optional[str],
                    tier: Optional[str]):
    """Search the blueprint registry.

    Example:
        plaidify registry search energy
        plaidify registry search --tag utilities
        plaidify registry search --tier certified
    """
    client = _get_client(ctx.obj["server"], ctx.obj["api_key"])

    async def _search():
        try:
            params = {}
            if query:
                params["q"] = query
            if tag:
                params["tag"] = tag
            if tier:
                params["tier"] = tier

            r = await client._http.get("/registry/search", params=params)
            if r.status_code != 200:
                _echo_error(f"Search failed: {r.text}")
                sys.exit(1)
            data = r.json()

            results = data.get("results", [])
            count = data.get("count", 0)

            if count == 0:
                _echo_warn("No blueprints found.")
                return

            click.echo()
            click.echo(f"  Registry Results ({count}):")
            click.echo(f"  {'─' * 60}")
            for bp in results:
                tier_badge = click.style(f" [{bp['quality_tier']}]", fg="blue")
                mfa_badge = click.style(" [MFA]", fg="yellow") if bp.get("has_mfa") else ""
                tags = click.style(f" ({', '.join(bp['tags'])})", fg="bright_black") if bp.get("tags") else ""
                click.echo(f"  {click.style(bp['site'], bold=True, fg='green')}{tier_badge}{mfa_badge}{tags}")
                click.echo(f"    {bp['name']} — {bp['domain']}  (v{bp['version']}, {bp.get('downloads', 0)} downloads)")
            click.echo()
        except Exception as e:
            _echo_error(str(e))
            sys.exit(1)
        finally:
            await client.close()

    _run_async(_search())


@registry.command("install")
@click.argument("site")
@click.option("-o", "--output", default=None, type=click.Path(), help="Output file path.")
@click.pass_context
def registry_install(ctx: click.Context, site: str, output: Optional[str]):
    """Download a blueprint from the registry and save it locally.

    Example:
        plaidify registry install greengrid_energy
        plaidify registry install greengrid_energy -o ./connectors/greengrid.json
    """
    client = _get_client(ctx.obj["server"], ctx.obj["api_key"])

    async def _install():
        try:
            _echo_info(f"Downloading blueprint: {site}")
            r = await client._http.get(f"/registry/{site}")
            if r.status_code == 404:
                _echo_error(f"Blueprint '{site}' not found in registry.")
                sys.exit(1)
            if r.status_code != 200:
                _echo_error(f"Download failed: {r.text}")
                sys.exit(1)

            data = r.json()
            blueprint_data = data.get("blueprint", {})

            # Determine output path
            if output:
                out_path = Path(output)
            else:
                connectors_dir = Path("connectors")
                connectors_dir.mkdir(exist_ok=True)
                out_path = connectors_dir / f"{site}.json"

            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "w") as f:
                json.dump(blueprint_data, f, indent=2)

            _echo_success(f"Installed to {out_path}")
            click.echo(f"    Name:      {data.get('name', 'N/A')}")
            click.echo(f"    Version:   v{data.get('version', '?')}")
            click.echo(f"    Tier:      {data.get('quality_tier', 'N/A')}")
            click.echo(f"    Downloads: {data.get('downloads', 0)}")
            click.echo()
        except Exception as e:
            _echo_error(str(e))
            sys.exit(1)
        finally:
            await client.close()

    _run_async(_install())


@registry.command("publish")
@click.argument("filepath", type=click.Path(exists=True))
@click.option("--description", "-d", default="", help="Description for the blueprint.")
@click.pass_context
def registry_publish(ctx: click.Context, filepath: str, description: str):
    """Publish a blueprint to the registry.

    Requires authentication (--api-key or PLAIDIFY_API_KEY).

    Example:
        plaidify registry publish ./connectors/my_site.json -d "My utility provider"
    """
    if not ctx.obj.get("api_key"):
        _echo_error("Authentication required. Provide --api-key or set PLAIDIFY_API_KEY.")
        sys.exit(1)

    path = Path(filepath)
    try:
        with open(path) as f:
            blueprint_data = json.load(f)
    except json.JSONDecodeError as e:
        _echo_error(f"Invalid JSON: {e}")
        sys.exit(1)

    client = _get_client(ctx.obj["server"], ctx.obj["api_key"])

    async def _publish():
        try:
            _echo_info(f"Publishing {path.name}...")
            r = await client._http.post("/registry/publish", json={
                "blueprint": blueprint_data,
                "description": description,
            })
            if r.status_code == 422:
                _echo_error(f"Validation error: {r.json().get('detail', r.text)}")
                sys.exit(1)
            if r.status_code == 403:
                _echo_error(r.json().get("detail", "Forbidden"))
                sys.exit(1)
            if r.status_code != 200:
                _echo_error(f"Publish failed: {r.text}")
                sys.exit(1)

            data = r.json()
            status = data.get("status", "unknown")
            _echo_success(f"Blueprint {status}: {data.get('site', 'N/A')} (v{data.get('version', '?')})")
            click.echo()
        except Exception as e:
            _echo_error(str(e))
            sys.exit(1)
        finally:
            await client.close()

    _run_async(_publish())


# ── plaidify serve ───────────────────────────────────────────────────────────


@cli.command()
@click.option("--host", default="0.0.0.0", help="Bind address.")
@click.option("--port", default=8000, type=int, help="Port number.")
@click.option("--reload", "do_reload", is_flag=True, help="Enable auto-reload for development.")
def serve(host: str, port: int, do_reload: bool):
    """Start the Plaidify API server.

    Example:
        plaidify serve --port 8000 --reload
    """
    root = _find_project_root()

    _ensure_demo_env()

    click.echo()
    click.secho("  ◆ Plaidify Server", bold=True)
    click.echo(f"  {'─' * 45}")
    click.echo(f"  API:     http://{host}:{port}")
    click.echo(f"  Docs:    http://{host}:{port}/docs")
    click.echo(f"  {'─' * 45}")
    click.echo()

    cmd = [
        sys.executable, "-m", "uvicorn", "src.main:app",
        "--host", host, "--port", str(port),
        "--log-level", "info",
    ]
    if do_reload:
        cmd.extend(["--reload"])

    os.execvp(sys.executable, cmd)


# ── plaidify demo ────────────────────────────────────────────────────────────


@cli.command()
@click.option("--no-browser", is_flag=True, help="Don't auto-open the browser.")
@click.option("--api-port", default=8000, type=int, help="Plaidify API port.")
@click.option("--site-port", default=8080, type=int, help="Example site port.")
def demo(no_browser: bool, api_port: int, site_port: int):
    """Launch the full Plaidify demo.

    Starts both the GreenGrid Energy portal and the Plaidify API,
    then opens the demo UI in your browser.

    Example:
        plaidify demo
    """
    root = _find_project_root()

    _ensure_demo_env()

    procs = []

    try:
        click.echo()
        click.secho("  ◆ Plaidify Demo", bold=True)
        click.echo(f"  {'─' * 50}")

        # Start example site
        _echo_info("Starting GreenGrid Energy portal...")
        example_proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "example_site.server:app",
             "--host", "0.0.0.0", "--port", str(site_port),
             "--log-level", "warning"],
            cwd=str(root),
            env=os.environ.copy(),
        )
        procs.append(example_proc)
        time.sleep(1)

        # Start Plaidify API
        _echo_info("Starting Plaidify API...")
        api_proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "src.main:app",
             "--host", "0.0.0.0", "--port", str(api_port),
             "--log-level", "info"],
            cwd=str(root),
            env=os.environ.copy(),
        )
        procs.append(api_proc)
        time.sleep(1)

        demo_url = f"http://localhost:{api_port}/ui/demo.html"
        click.echo(f"  {'─' * 50}")
        click.echo(f"  Demo UI:        {demo_url}")
        click.echo(f"  API Docs:       http://localhost:{api_port}/docs")
        click.echo(f"  Utility Portal: http://localhost:{site_port}")
        click.echo(f"  {'─' * 50}")
        click.echo("  Press Ctrl+C to stop\n")

        if not no_browser:
            webbrowser.open(demo_url)

        # Wait
        while True:
            for p in procs:
                if p.poll() is not None:
                    raise KeyboardInterrupt
            time.sleep(0.5)

    except KeyboardInterrupt:
        click.echo("\n  Shutting down...")
    finally:
        for p in procs:
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:
                p.kill()
        _echo_success("Done.")
        click.echo()


# ── Utilities ─────────────────────────────────────────────────────────────────


def _ensure_demo_env():
    """Set required environment variables for demo mode if not already set."""
    os.environ.setdefault("ENCRYPTION_KEY", "demo-key-not-for-production-use!!")
    os.environ.setdefault("JWT_SECRET_KEY", "demo-jwt-secret-not-for-production")
    os.environ.setdefault("DATABASE_URL", "sqlite:///./plaidify_demo.db")
    os.environ.setdefault("DEBUG", "true")
    os.environ.setdefault("LOG_LEVEL", "INFO")
    os.environ.setdefault("CORS_ORIGINS", "*")
    os.environ.setdefault("BROWSER_HEADLESS", "true")


# ── plaidify rotate-key ──────────────────────────────────────────────────────


@cli.command("rotate-key")
@click.option("--old-key", required=True, envvar="ENCRYPTION_KEY_PREVIOUS",
              help="Current (old) master encryption key (base64url).")
@click.option("--new-key", required=True, envvar="ENCRYPTION_KEY",
              help="New master encryption key (base64url).")
@click.option("--re-encrypt", is_flag=True, default=False,
              help="Also re-encrypt AccessToken credentials (not just DEK re-wrap).")
@click.option("--batch-size", default=100, type=int,
              help="Number of tokens per re-encryption batch.")
def rotate_key(old_key: str, new_key: str, re_encrypt: bool, batch_size: int):
    """Rotate the master encryption key.

    Re-wraps all per-user DEKs from the old master key to the new one.
    Optionally re-encrypts stored credentials and bumps key_version.

    Procedure:
      1. Generate a new key:
           python -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
      2. Run rotation:
           plaidify rotate-key --old-key <CURRENT_KEY> --new-key <NEW_KEY> --re-encrypt
      3. Update .env: set ENCRYPTION_KEY=<NEW_KEY>,
           ENCRYPTION_KEY_PREVIOUS=<OLD_KEY>, bump ENCRYPTION_KEY_VERSION.
    """
    # Import server-side modules (requires src/ on path)
    root = _find_project_root()
    sys.path.insert(0, str(root))

    click.echo()
    click.secho("  ◆ Plaidify Key Rotation", bold=True)
    click.echo(f"  {'─' * 45}")

    try:
        from src.database import (
            rotate_master_key,
            re_encrypt_tokens,
            SessionLocal,
        )

        db = SessionLocal()
        try:
            # Step 1: Re-wrap DEKs
            _echo_info("Re-wrapping user DEKs with new master key...")
            dek_count = rotate_master_key(old_key, new_key, db)
            _echo_success(f"Re-wrapped {dek_count} DEK(s)")

            # Step 2: Optionally re-encrypt AccessToken credentials
            if re_encrypt:
                _echo_info("Re-encrypting access token credentials...")
                total = 0
                while True:
                    count = re_encrypt_tokens(db, batch_size=batch_size)
                    total += count
                    if count < batch_size:
                        break
                _echo_success(f"Re-encrypted {total} access token(s)")

            click.echo(f"  {'─' * 45}")
            _echo_success("Key rotation complete!")
            click.echo()
            _echo_info("Next steps:")
            click.echo("    1. Set ENCRYPTION_KEY=<new key> in .env")
            click.echo("    2. Set ENCRYPTION_KEY_PREVIOUS=<old key> in .env")
            click.echo("    3. Increment ENCRYPTION_KEY_VERSION in .env")
            click.echo("    4. Restart the server")
            click.echo()
        finally:
            db.close()

    except Exception as e:
        _echo_error(f"Key rotation failed: {e}")
        sys.exit(1)


# ── Entry point ──────────────────────────────────────────────────────────────


def main():
    """CLI entry point."""
    cli()


if __name__ == "__main__":
    main()
