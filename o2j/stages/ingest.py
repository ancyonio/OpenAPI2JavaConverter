"""S0 Ingest - fetch, size-check, hash and quarantine the input spec.

The spec is untrusted. This stage copies it into ``work/00-ingest/``, which holds no scripts
and is never used as a working directory, and records its hash so later stages can prove they
operated on the bytes that were checked.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from pydantic import BaseModel, Field

from o2j.config import O2JConfig
from o2j.errors import IngestError
from o2j.specdoc import detect_version, load_spec, sha256_file
from o2j.workdir import WorkDir


class IngestResult(BaseModel):
    source: str
    path: str
    raw_hash: str = Field(alias="rawHash")
    size_bytes: int = Field(alias="sizeBytes")
    detected_version: str = Field(alias="detectedVersion")

    model_config = {"populate_by_name": True}


def ingest(
    cfg: O2JConfig,
    source: str | Path | None = None,
    work: WorkDir | None = None,
) -> IngestResult:
    """Copy the spec into quarantine and record its identity."""
    work = work or WorkDir()
    origin = Path(source) if source is not None else cfg.project_root / cfg.openapi.input

    if str(origin).startswith(("http://", "https://")):
        # Deliberately not implemented: a remote fetch would make the build depend on the
        # network and on whatever the far end serves today. Specs are vendored instead.
        raise IngestError(
            f"remote spec URIs are not supported: {origin}",
            hint="download the spec, commit it under openapi/, and point o2j.yaml at it",
        )

    if not origin.is_file():
        raise IngestError(
            f"spec not found: {origin}",
            hint=f"set `openapi.input` in {cfg.source_path or 'o2j.yaml'}",
        )

    size = origin.stat().st_size
    if size > cfg.openapi.max_document_bytes:
        raise IngestError(
            f"spec is {size} bytes, over the {cfg.openapi.max_document_bytes} byte cap",
            hint="raise `openapi.max_document_bytes` if this document is genuinely this large",
        )
    if size == 0:
        raise IngestError(f"spec is empty: {origin}")

    if origin.suffix.lower() not in (".yaml", ".yml", ".json"):
        raise IngestError(
            f"unexpected spec extension {origin.suffix!r}; expected .yaml, .yml or .json"
        )

    target = work.path("ingest", "spec.raw.yaml")
    shutil.copyfile(origin, target)

    # Parse once here so a malformed document fails at ingest rather than three stages later.
    doc = load_spec(target)
    version = detect_version(doc)

    return IngestResult(
        source=str(origin),
        path=str(target),
        raw_hash=sha256_file(target),
        size_bytes=size,
        detected_version=version,
    )
