"""The staging area where pipeline artifacts live.

Each stage writes its artifact to a numbered subdirectory, so the state of a run is legible
from the filesystem alone and a stage can be re-run from the previous stage's output without
replaying the whole pipeline.

``00-ingest`` is a quarantine directory: the untrusted spec lands there, and nothing is ever
executed from it (SPECIFICATION.md Section 5.3).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from o2j.errors import O2JError

T = TypeVar("T", bound=BaseModel)

STAGE_DIRS: dict[str, str] = {
    "ingest": "00-ingest",
    "validate": "01-validate",
    "normalize": "02-normalize",
    "plan": "03-plan",
    "generate": "04-generate",
    "test": "06-test",
    "gate": "07-gate",
    "report": "08-report",
}


class WorkDir:
    """Resolves and creates the per-stage artifact directories."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(
            root if root is not None else os.environ.get("O2J_WORK_DIR") or "work"
        ).resolve()

    def stage(self, name: str, *, create: bool = True) -> Path:
        if name not in STAGE_DIRS:
            raise O2JError(f"unknown stage {name!r}")
        path = self.root / STAGE_DIRS[name]
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def path(self, stage: str, filename: str, *, create: bool = True) -> Path:
        return self.stage(stage, create=create) / filename

    def write_json(self, stage: str, filename: str, model: BaseModel) -> Path:
        """Serialise an artifact with stable key order and a trailing newline.

        ``sort_keys`` is not used: the models' field order is meaningful for readability, and
        Pydantic already emits it deterministically.
        """
        target = self.path(stage, filename)
        payload = model.model_dump(by_alias=True, exclude_none=False)
        target.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return target

    def read_json(self, stage: str, filename: str, model_type: type[T]) -> T:
        target = self.path(stage, filename, create=False)
        if not target.is_file():
            raise O2JError(
                f"missing artifact {target}",
                hint=f"run the {stage} stage first",
            )
        return model_type.model_validate_json(target.read_text(encoding="utf-8"))

    def write_text(self, stage: str, filename: str, text: str) -> Path:
        target = self.path(stage, filename)
        # Always LF: a CRLF artifact on Windows would hash differently from the same run on
        # Linux CI and break the determinism gate for no real reason.
        target.write_text(text.replace("\r\n", "\n"), encoding="utf-8", newline="\n")
        return target
