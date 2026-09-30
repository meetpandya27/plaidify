"""The hash-locked requirement files must match requirements*.txt.

The image and CI install the .lock files, so a pin changed only in a .txt file
(by hand or by Dependabot) would never be built or tested.
"""

import importlib.util
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_requirements_locks.py"
FILES = (
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-kms.txt",
    "requirements.lock",
    "requirements-dev.lock",
    "requirements-kms.lock",
)


def _checker():
    spec = importlib.util.spec_from_file_location("check_requirements_locks", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _copy_repo_files(tmp_path):
    for name in FILES:
        shutil.copy(REPO_ROOT / name, tmp_path / name)


def test_repository_locks_match_their_sources():
    assert _checker().check(REPO_ROOT) == []


def test_a_pin_bumped_only_in_the_txt_file_is_reported(tmp_path):
    _copy_repo_files(tmp_path)
    source = tmp_path / "requirements.txt"
    source.write_text(source.read_text().replace("click==8.3.3", "click==8.3.4"))

    problems = _checker().check(tmp_path)

    assert any("requirements.lock" in p and "click" in p for p in problems)
    # requirements-dev.txt includes requirements.txt, so its lock is stale too.
    assert any("requirements-dev.lock" in p and "click" in p for p in problems)


def test_a_dependency_missing_from_a_lock_is_reported(tmp_path):
    _copy_repo_files(tmp_path)
    dev = tmp_path / "requirements-dev.txt"
    dev.write_text(dev.read_text() + "\nhypothesis==6.100.0\n")

    problems = _checker().check(tmp_path)

    assert problems == ["requirements-dev.lock: hypothesis (requirements-dev.txt) is not locked"]


def test_locks_that_disagree_are_reported(tmp_path):
    _copy_repo_files(tmp_path)
    lock = tmp_path / "requirements-dev.lock"
    text = lock.read_text()
    version = next(line for line in text.splitlines() if line.startswith("fastapi==")).split("==")[1].split()[0]
    lock.write_text(text.replace(f"fastapi=={version}", "fastapi==0.1.0", 1))

    problems = _checker().check(tmp_path)

    assert any("fastapi==0.1.0 but requirements.lock has" in p for p in problems)
