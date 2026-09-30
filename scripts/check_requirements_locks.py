#!/usr/bin/env python3
"""Fail when the hash-locked requirement files no longer match their sources.

The image and CI install requirements*.lock; people and Dependabot edit
requirements*.txt. After an edit to a .txt file, this reports every pin the
matching .lock doesn't carry and prints the command that regenerates it.

    python scripts/check_requirements_locks.py        # from the repo root

Checks:
  requirements.txt      -> requirements.lock      (every `==` pin, same version)
  requirements-dev.txt  -> requirements-dev.lock  (its pins plus requirements.txt's)
  requirements-kms.txt  -> requirements-kms.lock  (each range is satisfied)
and that the dev and KMS locks agree with requirements.lock on shared packages.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
except ImportError:  # pragma: no cover - packaging ships with pip/setuptools envs
    print("check_requirements_locks: needs the 'packaging' package", file=sys.stderr)
    sys.exit(2)

COMPILE = "pip-compile --generate-hashes --allow-unsafe --strip-extras"
REGENERATE = {
    "requirements.lock": f"{COMPILE} --output-file=requirements.lock requirements.txt",
    "requirements-dev.lock": f"{COMPILE} --output-file=requirements-dev.lock requirements-dev.txt",
    "requirements-kms.lock": (
        f"{COMPILE} --constraint=requirements.lock --output-file=requirements-kms.lock requirements-kms.txt"
    ),
}
_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._\-]*)==([^\s\\;]+)")


def read_requirements(path: Path) -> list[Requirement]:
    """Requirements from a .txt file, following `-r` includes."""
    out: list[Requirement] = []
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith(("-r ", "--requirement ")):
            out.extend(read_requirements(path.parent / line.split(maxsplit=1)[1]))
        elif not line.startswith("-"):
            out.append(Requirement(line))
    return out


def read_lock(path: Path) -> dict[str, str]:
    pins = {}
    for line in path.read_text().splitlines():
        match = _PIN.match(line)
        if match:
            pins[canonicalize_name(match.group(1))] = match.group(2)
    return pins


def check(root: Path) -> list[str]:
    problems: list[str] = []
    locks = {name: read_lock(root / name) for name in REGENERATE if (root / name).exists()}
    for name in REGENERATE:
        if name not in locks:
            problems.append(f"{name} is missing")

    pairs = (
        ("requirements.txt", "requirements.lock"),
        ("requirements-dev.txt", "requirements-dev.lock"),
        ("requirements-kms.txt", "requirements-kms.lock"),
    )
    for source, lock in pairs:
        if lock not in locks or not (root / source).exists():
            continue
        pinned = locks[lock]
        for req in read_requirements(root / source):
            name = canonicalize_name(req.name)
            locked = pinned.get(name)
            if locked is None:
                problems.append(f"{lock}: {req.name} ({source}) is not locked")
            elif not req.specifier.contains(locked, prereleases=True):
                problems.append(f"{lock}: {req.name}=={locked} does not satisfy {source}'s {req}")

    base = locks.get("requirements.lock", {})
    for lock in ("requirements-dev.lock", "requirements-kms.lock"):
        for name, version in locks.get(lock, {}).items():
            if name in base and base[name] != version:
                problems.append(f"{lock}: {name}=={version} but requirements.lock has {base[name]}")
    return problems


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
    problems = check(root)
    if not problems:
        print("requirement locks match requirements*.txt")
        return 0
    print("The hash-locked requirement files are out of date:", file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    print("\nRegenerate them with pip-tools under Python 3.11 (the image's Python):", file=sys.stderr)
    for command in REGENERATE.values():
        print(f"  {command}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
