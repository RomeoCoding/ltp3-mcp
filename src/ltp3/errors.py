"""Error type and the error-code vocabulary of PLAN.md section 4."""

from __future__ import annotations

NOT_FOUND = "NOT_FOUND"
BAD_PATH = "BAD_PATH"
PATH_NOT_ALLOWED = "PATH_NOT_ALLOWED"
PARSE_ERROR = "PARSE_ERROR"
UNKNOWN_TREE = "UNKNOWN_TREE"
UNKNOWN_TAB = "UNKNOWN_TAB"
UNKNOWN_PART = "UNKNOWN_PART"
AMBIGUOUS_PART = "AMBIGUOUS_PART"
UNKNOWN_TERMINAL = "UNKNOWN_TERMINAL"
BAD_VALUE = "BAD_VALUE"
UNKNOWN_KEY = "UNKNOWN_KEY"
TERMINAL_IN_USE = "TERMINAL_IN_USE"
WOULD_SHORT = "WOULD_SHORT"
CYCLE = "CYCLE"
READ_ONLY = "READ_ONLY"
VALIDATION_FAILED = "VALIDATION_FAILED"
FILE_EXISTS = "FILE_EXISTS"
STALE_FILE = "STALE_FILE"
UNPARSEABLE_ARGS = "UNPARSEABLE_ARGS"
NOTHING_TO_UNDO = "NOTHING_TO_UNDO"

ALL_CODES: tuple[str, ...] = (
    NOT_FOUND, BAD_PATH, PATH_NOT_ALLOWED, PARSE_ERROR, UNKNOWN_TREE, UNKNOWN_TAB,
    UNKNOWN_PART, AMBIGUOUS_PART, UNKNOWN_TERMINAL, BAD_VALUE, UNKNOWN_KEY, TERMINAL_IN_USE,
    WOULD_SHORT, CYCLE, READ_ONLY, VALIDATION_FAILED, FILE_EXISTS, STALE_FILE,
    UNPARSEABLE_ARGS, NOTHING_TO_UNDO,
)  # fmt: skip


class Ltp3Error(Exception):
    """Domain error; `server.py` maps it to an MCP tool error carrying `to_dict()` as JSON."""

    def __init__(self, code: str, message: str, hint: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.hint = hint

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message, "hint": self.hint}
