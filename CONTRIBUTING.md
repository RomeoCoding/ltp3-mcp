# Contributing

- **Clean-room rule.** Format and solver knowledge comes from `.ltp3`/`.ltp3t` data files and from results you read off the application's own screen. Do not decompile, disassemble or string-dump ADI binaries, DLLs or config files, and do not paste anything obtained that way.
- **Never attach factory templates** to issues or pull requests, and never commit ADI files. Oracle contributions must be trees you designed yourself, with the application's Calculate summary recorded by hand.
- **Tests first.** Add a failing test, then the fix. `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy --strict src` must pass.
- Corpus tests read templates in place (`LTP3_TEMPLATE_DIR`) and must skip when they are absent.
