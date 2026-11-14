# Deviations from PLAN.md

Each entry: what the plan said, what was done, why.

## P0

1. `requires-python = ">=3.11"` instead of `>=3.10`. The pinned `hypothesis==6.170.15` requires
   3.11, so `uv sync --dev` cannot resolve a 3.10 split. The MCP SDK itself would allow 3.10.
2. No `[project.scripts]` entry yet. `ltp3-mcp = "ltp3.server:main"` belongs to P7 (which owns
   `server.py`); a script pointing at a missing module would be a dangling entry point.
3. `docs/PLAN.md` and `docs/FORMAT.md` were committed unmodified in the first P0 commit so the repo
   carries its own spec. Phase commits are two: failing tests first, then implementation, to keep
   the test-before-implementation order visible (QA checklist, section 8).
4. `check_writable` raises `BAD_PATH` for every non-`.ltp3` target including `.ltp3t` (section 4
   lists `BAD_PATH` for "not .ltp3" and has no code for a read-only extension). It also refuses
   anything under `ProgramFiles` / `ProgramFiles(x86)` in addition to the ADI tree, because P5 needs
   "a path under Program Files" refused.
5. `tests/conftest.py` also provides a `sample_paths` fixture (both samples) because P1's
   `test_roundtrip_samples` needs both and P1 does not own `conftest.py`.
6. No git identity is configured on this machine; commits were made with
   `-c user.name=Claude -c user.email=noreply@anthropic.com`, not written to any git config.
