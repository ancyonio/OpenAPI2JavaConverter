---
applyTo: "o2j/**/*.py"
---

# o2j Python toolchain

- Python >= 3.11. `ruff` + `mypy --strict` clean.
- Pydantic v2 models at every artifact boundary; every artifact has a JSON Schema in
  `o2j/schemas/`. Never return an untyped dict from a stage.
- Stage functions are pure with respect to their declared inputs and outputs. No stage reads
  another stage's internals; they communicate through artifacts.
- `cli.py` and `mcp_server.py` are thin adapters. Logic lives in `o2j/stages/`. Anything you
  add to an adapter is unreachable from the other face.
- Structured JSON to stdout, human text to stderr. Agents parse stdout.
- Exit codes per `SPECIFICATION.md` §8.4, via `o2j.errors.ExitCode`. Never invent a new one.
- YAML via `ruamel.yaml` with deterministic key ordering — output must be byte-stable.
- Spec input is untrusted: no `yaml.load` without a safe loader, no `eval`, no shelling out with
  spec-derived strings, and never run an interpreter with the ingest directory as cwd.
- Every new rule needs a golden-corpus fixture in `tests/corpus/` that fails without it.
