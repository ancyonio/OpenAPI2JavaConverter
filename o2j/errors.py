"""Exit codes and the exception hierarchy that maps onto them.

Exit codes are part of the tool's contract with CI and with Copilot agents, which branch on
them. They are defined in SPECIFICATION.md Section 8.4 and must not be extended ad hoc: an
agent that sees an unknown code cannot tell which stage failed.
"""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    """Process exit codes. See SPECIFICATION.md Section 8.4."""

    OK = 0
    INTERNAL_ERROR = 1
    USAGE = 2
    VALIDATION_FAILED = 3
    LOAD_BEARING_LOSS = 4
    GENERATION_FAILED = 5
    GATE_FAILED = 6
    DRIFT_FAILED = 7


class O2JError(Exception):
    """Base class for every expected failure.

    Carries the exit code so the CLI adapter never has to re-derive it from the exception
    type, and so the MCP adapter can report it as structured data instead of a traceback.
    """

    exit_code: ExitCode = ExitCode.INTERNAL_ERROR

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class ConfigError(O2JError):
    exit_code = ExitCode.USAGE


class IngestError(O2JError):
    exit_code = ExitCode.USAGE


class ValidationFailed(O2JError):
    exit_code = ExitCode.VALIDATION_FAILED


class LoadBearingLoss(O2JError):
    """A version downgrade dropped a construct that changes API behaviour.

    Kept distinct from ValidationFailed because the remedy is different: the spec is valid,
    but this pipeline cannot carry it to Java without changing what the API does.
    """

    exit_code = ExitCode.LOAD_BEARING_LOSS


class GenerationFailed(O2JError):
    exit_code = ExitCode.GENERATION_FAILED


class GateFailed(O2JError):
    exit_code = ExitCode.GATE_FAILED


class DriftFailed(O2JError):
    exit_code = ExitCode.DRIFT_FAILED
