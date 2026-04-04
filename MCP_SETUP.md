# MCP (Model Context Protocol) Setup

## Brave Search MCP

The `.mcp.json` file in this repo configures the Brave Search MCP server for
Claude Code (ForgeCode). It is loaded automatically when Claude Code starts in
this directory.

### Reloading MCP servers in Claude Code

If you edit `.mcp.json` while Claude Code is running:

1. Type `/mcp` in the Claude Code prompt to see the current MCP status.
2. Restart Claude Code (`Ctrl-C` then re-launch) to pick up changes.
3. Alternatively, use the command palette or `/mcp` to reconnect servers.

### Inspecting available tools

After MCP servers are connected, Claude Code can use tools they expose.
Run `/mcp` to list connected servers and their available tools. For Brave
Search this typically provides `brave_web_search` and `brave_local_search`.

### Prerequisites

- Node.js >= 18 (npx must be on PATH)
- A valid `BRAVE_API_KEY` in your `.env` file
