# MCP server

Requires the `[mcp]` extra (see the README's
[installation section](../README.md#installation)).
Start with `korvid --mcp` (or set `mcp: {enabled: true}` in
`~/.config/korvid/config.yaml`) to expose the read and UI-drive tools to
external agents — VS Code Copilot Chat, Claude Code, Cursor, Zed — over a
[Streamable HTTP MCP](https://modelcontextprotocol.io) server bound to
`127.0.0.1:7878` (`mcp: {port: N}` to change).  External hosts can list
resources, fetch manifests, logs, and events, and drive the TUI you are
looking at (navigate, filter, open logs/describe).  Write tools are **not**
exposed: cluster mutations stay behind the in-TUI confirmation dialog.

The live endpoint is also published to
`$XDG_STATE_HOME/korvid/mcp-endpoint.json` (defaulting to
`~/.local/state/korvid/mcp-endpoint.json` when `XDG_STATE_HOME` is unset)
while korvid runs.  The file is a registry keyed by process id, so multiple
korvid instances can be discovered side by side:

```json
{"servers": {"12345": {"url": "http://127.0.0.1:7878/mcp", "port": 7878, "pid": 12345}}}
```

## Host configuration

**VS Code** (`.vscode/mcp.json`):

```json
{"servers": {"korvid": {"type": "http", "url": "http://127.0.0.1:7878/mcp"}}}
```

**Claude Code:**

```sh
claude mcp add --transport http korvid http://127.0.0.1:7878/mcp
```

**Cursor** (`.cursor/mcp.json`):

```json
{"mcpServers": {"korvid": {"type": "http", "url": "http://127.0.0.1:7878/mcp"}}}
```

**Zed** (`settings.json`):

```json
{"context_servers": {"korvid": {"url": "http://127.0.0.1:7878/mcp"}}}
```
