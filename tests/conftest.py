"""Shared fixtures.

Every test runs against the golden corpus in ``tests/corpus``. A rule change that alters a
golden outcome must update its fixture in the same commit, so the diff is the reviewable
record of the behaviour change.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from o2j.config import O2JConfig, load_config
from o2j.workdir import WorkDir

REPO_ROOT = Path(__file__).resolve().parent.parent
CORPUS = REPO_ROOT / "tests" / "corpus"


@pytest.fixture
def cfg() -> O2JConfig:
    """The repository's own o2j.yaml, which doubles as the reference configuration."""
    return load_config(REPO_ROOT / "o2j.yaml")


@pytest.fixture
def work(tmp_path: Path) -> WorkDir:
    """A throwaway staging area, so tests never touch the repository's work/ directory."""
    return WorkDir(tmp_path / "work")


@pytest.fixture
def corpus() -> Path:
    return CORPUS


def spec(name: str) -> Path:
    """Path to a corpus fixture's spec."""
    path = CORPUS / name / "spec.yaml"
    if not path.is_file():
        raise FileNotFoundError(path)
    return path
