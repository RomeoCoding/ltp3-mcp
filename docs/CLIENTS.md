# Client configuration

`ltp3-mcp` is a stdio MCP server. Install once from the repository:

```
uv sync
```

Environment variables (all optional):

| Variable | Meaning |
|---|---|
| `LTP3_TEMPLATE_DIR` | folder with the factory `.ltp3t` templates (default: the standard LTpowerPlanner III install path) |
| `LTP3_ALLOWED_DIRS` | `;`-separated folders `save_tree` may write to (default: the server's working directory) |

Writes are never allowed inside Program Files or the LTpowerPlanner install tree.
Replace `C:\path\to\ltp3-mcp` below with your checkout.

## Claude Code

```
claude mcp add ltp3 --scope user -e LTP3_ALLOWED_DIRS=C:\work\power-trees -- uv run --directory C:\path\to\ltp3-mcp ltp3-mcp
```

Check with `claude mcp list`, or `/mcp` inside a session.

## Claude Desktop

Edit `%APPDATA%\Claude\claude_desktop_config.json` and restart Claude Desktop:

```json
{
  "mcpServers": {
    "ltp3": {
      "command": "uv",
      "args": ["run", "--directory", "C:\\path\\to\\ltp3-mcp", "ltp3-mcp"],
      "env": { "LTP3_ALLOWED_DIRS": "C:\\work\\power-trees" }
    }
  }
}
```

## Generic stdio client

Command: `uv run --directory C:\path\to\ltp3-mcp ltp3-mcp` (or `C:\path\to\ltp3-mcp\.venv\Scripts\ltp3-mcp.exe`
after `uv sync`). Transport: stdio, newline-delimited JSON-RPC. Smoke test by hand:

```
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
```

The second response lists 22 tools. `ltp3-mcp --help` prints usage and exits 0.
