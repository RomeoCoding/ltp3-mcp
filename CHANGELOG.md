# Changelog

## 0.1.0

First release.

- Lossless reader and writer for `.ltp3` and `.ltp3t`: all 261 factory templates round-trip byte-exact.
- Typed model, net graph, validator, semantic differ and neutral JSON export.
- Solver matching the application's Calculate on seven oracle trees; 382 of 444 corpus tabs solve completely. Unsupported constructs are reported as blocked, assumptions are listed in every result.
- Safe editor and writer: validation before save, atomic write, backup on overwrite, no overwrite by default, install folder refused.
- MCP server with 22 tools (stdio), and the `ltp3` command line (`solve`, `validate`, `diff`, `export`, `roundtrip`, `templates`) with CI-usable exit codes.
