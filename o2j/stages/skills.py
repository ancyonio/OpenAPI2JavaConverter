"""Publish the root `skills/` directory to the paths each agent runtime scans.

`skills/` at the repository root is the canonical copy: it is where skills are edited, and it
is the unit you copy into another repository or publish with `gh skill`. It is not, however, a
location any runtime reads.

The runtimes look in fixed, vendor-specific directories. This project is a GitHub Copilot
solution, so it mirrors to Copilot's native location only:

* `.github/skills/` - GitHub Copilot (its native location, and the one Copilot code review
  looks in)

GitHub also documents `.claude/skills/` and `.agents/skills/` as Copilot locations, and
`.claude/skills/` is the only directory Claude Code reads. Adding either to MIRRORS below is a
one-line change if another runtime needs to see these skills; nothing else has to move, because
`skills/` stays canonical either way.

No runtime reads a plain `skills/`, and a symlink is not a portable answer: git stores symlinks
only conditionally, and on Windows a checkout can materialise them as text files containing a
path. So the mirror is a real directory, written by this module.

Duplication is a drift risk, which is exactly what skills exist to avoid - so `check()` asserts
byte-identity and the test suite runs it. A mirror that falls behind fails the build rather
than quietly serving the runtime stale guidance while the canonical copy has moved on.
"""

from __future__ import annotations

import filecmp
import shutil
from pathlib import Path

from pydantic import BaseModel, Field

from o2j.errors import DriftFailed

# Canonical source, relative to the project root.
CANONICAL = "skills"

# Where the runtime actually looks. Copilot's native location; see the module docstring for
# the other documented paths and what adding one costs.
MIRRORS: tuple[str, ...] = (".github/skills",)


class SkillsSyncResult(BaseModel):
    canonical: str
    mirrors: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    files_written: int = Field(default=0, alias="filesWritten")
    removed: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


class SkillsCheckResult(BaseModel):
    in_sync: bool = Field(alias="inSync")
    differences: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


def _canonical_dir(root: Path) -> Path:
    source = root / CANONICAL
    if not source.is_dir():
        raise DriftFailed(
            f"no canonical skills directory at {source}",
            hint="skills live in `skills/` at the repository root",
        )
    return source


def _relative_files(directory: Path) -> list[Path]:
    return sorted(
        p.relative_to(directory) for p in directory.rglob("*") if p.is_file()
    )


def sync(root: str | Path = ".") -> SkillsSyncResult:
    """Copy `skills/` into every runtime location, removing anything stale.

    Stale removal matters: a renamed or deleted skill that lingers in a mirror keeps being
    offered to one runtime after it has gone from the source.
    """
    project = Path(root).resolve()
    source = _canonical_dir(project)

    skills = sorted(p.name for p in source.iterdir() if p.is_dir())
    written = 0
    removed: list[str] = []
    mirrors: list[str] = []

    for mirror_path in MIRRORS:
        target = project / mirror_path
        target.mkdir(parents=True, exist_ok=True)
        mirrors.append(mirror_path)

        wanted = set(_relative_files(source))

        for relative in sorted(wanted):
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            origin = source / relative
            if destination.is_file() and filecmp.cmp(origin, destination, shallow=False):
                continue
            shutil.copyfile(origin, destination)
            written += 1

        for existing in _relative_files(target):
            if existing not in wanted:
                (target / existing).unlink()
                removed.append(f"{mirror_path}/{existing.as_posix()}")

        # Clear out directories left empty by a removal, so a deleted skill leaves no trace.
        for directory in sorted(
            (p for p in target.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)
        ):
            if not any(directory.iterdir()):
                directory.rmdir()

    return SkillsSyncResult(
        canonical=CANONICAL,
        mirrors=mirrors,
        skills=skills,
        files_written=written,
        removed=removed,
    )


def check(root: str | Path = ".") -> SkillsCheckResult:
    """Assert every mirror is byte-identical to `skills/`."""
    project = Path(root).resolve()
    source = _canonical_dir(project)
    expected = set(_relative_files(source))
    differences: list[str] = []

    for mirror_path in MIRRORS:
        target = project / mirror_path
        if not target.is_dir():
            differences.append(f"{mirror_path} is missing entirely")
            continue

        present = set(_relative_files(target))

        for relative in sorted(expected - present):
            differences.append(f"{mirror_path}/{relative.as_posix()} is missing")
        for relative in sorted(present - expected):
            differences.append(f"{mirror_path}/{relative.as_posix()} is stale")
        for relative in sorted(expected & present):
            if not filecmp.cmp(source / relative, target / relative, shallow=False):
                differences.append(f"{mirror_path}/{relative.as_posix()} differs")

    return SkillsCheckResult(in_sync=not differences, differences=differences)
