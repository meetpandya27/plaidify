"""Every setting is documented where operators look, and the env templates name nothing stale."""

import re
from pathlib import Path

from src.config import Settings

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = {name.upper() for name in Settings.model_fields}


def _assigned_names(relative_path: str) -> set[str]:
    """Names set in an env template, commented out (``# NAME=``) or not."""
    names = set()
    for line in (ROOT / relative_path).read_text().splitlines():
        match = re.match(r"\s*#?\s*([A-Z][A-Z0-9_]{2,})=", line)
        if match:
            names.add(match.group(1))
    return names


def _names_something_reads() -> set[str]:
    """Settings (and their aliases), direct environment reads in the Python code
    and the container entrypoint, Dockerfile build args and Compose interpolations."""
    names = set(SETTINGS)
    for field in Settings.model_fields.values():
        names.update(str(choice).upper() for choice in getattr(field.validation_alias, "choices", ()))
    env_read = re.compile(r"""os\.(?:environ\.get|getenv|environ\[)\(?\s*["']([A-Z][A-Z0-9_]+)["']""")
    for path in [*(ROOT / "src").rglob("*.py"), *(ROOT / "scripts").glob("*.py"), ROOT / "gunicorn.conf.py"]:
        names.update(env_read.findall(path.read_text()))
    names.update(re.findall(r"\$\{([A-Z][A-Z0-9_]+)", (ROOT / "scripts/container-entrypoint.sh").read_text()))
    names.update(re.findall(r"^ARG ([A-Z][A-Z0-9_]+)", (ROOT / "Dockerfile").read_text(), re.MULTILINE))
    for compose_file in ROOT.glob("docker-compose*.yml"):
        names.update(re.findall(r"\$\{([A-Z][A-Z0-9_]+)", compose_file.read_text()))
    return names


def test_every_setting_is_in_env_example():
    missing = SETTINGS - _assigned_names(".env.example")
    assert not missing, f"Add these settings to .env.example: {sorted(missing)}"


def test_every_setting_is_in_the_deployment_guide():
    documented = set(re.findall(r"`([A-Z][A-Z0-9_]{2,})`", (ROOT / "docs/DEPLOYMENT.md").read_text()))
    missing = SETTINGS - documented
    assert not missing, f"Add these settings to docs/DEPLOYMENT.md: {sorted(missing)}"


def test_env_templates_name_only_what_something_reads():
    known = _names_something_reads()
    for template in (".env.example", ".env.production.example"):
        stale = _assigned_names(template) - known
        assert not stale, f"{template} names variables nothing reads: {sorted(stale)}"
