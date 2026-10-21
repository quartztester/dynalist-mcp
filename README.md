# dynalist-mcp

An [MCP](https://modelcontextprotocol.io) (Model Context Protocol) server for the [Dynalist](https://dynalist.io) outliner. It lets MCP clients — Claude Desktop, Cursor, Windsurf, and any other MCP-capable agent — read, search, and edit your Dynalist documents, folders, and inbox.

Every endpoint of the official [Dynalist API v1](https://apidocs.dynalist.io/) is exposed, plus two quality-of-life helpers: an indented outline renderer that annotates each item with its node ID, and a full-text item search (the API itself has no search endpoint).

## Requirements

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) (recommended — no install step needed) or pip
- A Dynalist **API secret**: open <https://dynalist.io/developer> and copy it. This token grants full access to your account — treat it like a password and never commit it.

## Quick start

No clone or install required — point your MCP client at the Git repo via `uv` and it fetches and runs the server on demand:

```json
{
  "mcpServers": {
    "dynalist": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/quartztester/dynalist-mcp", "dynalist-mcp"],
      "env": { "DYNALIST_API_TOKEN": "your-api-secret-here" }
    }
  }
}
```

Or install locally:

```bash
git clone https://github.com/quartztester/dynalist-mcp.git
cd dynalist-mcp
uv venv && uv pip install -e .   # or: pip install -e .
export DYNALIST_API_TOKEN="your-…ere"
dynalist-mcp                     # runs the stdio server
```

Then configure your client with `"command": "dynalist-mcp"` (or the venv path).

### Where the config goes

| Client | Config file |
|---|---|
| Claude Desktop | `claude_desktop_config.json` (Settings → Developer) |
| Cursor | `.cursor/mcp.json` (project) or global MCP settings |
| Hermes Agent | `mcp_servers` section in `config.yaml` |
| Any other MCP client | stdio server: command + `DYNALIST_API_TOKEN` env var |

You can also put the token in a `.env` file (`cp .env.example .env`) when running the server directly from a shell that loads it.

## Tools

| Tool | What it does |
|---|---|
| `dynalist_list_files` | All documents/folders with IDs, types, permissions, and full paths |
| `dynalist_read_document` | One document: metadata, indented outline with `<node IDs>`, raw node list |
| `dynalist_find_items` | Substring search over item text + notes, in chosen docs or all docs |
| `dynalist_edit_document` | Batch `insert` / `edit` / `move` / `delete` of nodes in a document |
| `dynalist_edit_files` | `create` / `edit` (rename) / `move` documents and folders |
| `dynalist_add_to_inbox` | Quick-capture an item (with note, checkbox, heading, color) to the inbox |
| `dynalist_check_updates` | Cheap version-number check for a set of documents |
| `dynalist_upload_file` | Upload a base64 file, returns a `dynalist.io/u/...` URL (**Pro only**) |
| `dynalist_get_preference` / `dynalist_set_preference` | Account prefs (`inbox_location`, `inbox_move_position`) |

Typical agent flow: `list_files` → `read_document` (copy a `<node ID>` from the outline) → `edit_document`.

## Limitations

All of the following come from the [official API docs](https://apidocs.dynalist.io/) and are inherent to the Dynalist API, not bugs in this server.

### Rate limits (token bucket: sustained rate + burst)

| Endpoint (tool) | Sustained | Burst |
|---|---|---|
| `file/list` (`list_files`) | 6 req/min | 10 |
| `file/edit` (`edit_files`) | 60 req/min | 50 |
| `doc/read` (`read_document`, `find_items`) | 30 req/min | 100 |
| `doc/check_for_updates` (`check_updates`) | 60 req/min | 50 |
| `doc/edit` (`edit_document`) | 60 req/min | 20 |
| `doc/edit` changes (second, separate limit) | 240 changes/min | 500 |
| `inbox/add` (`add_to_inbox`) | 60 req/min | 20 |
| `upload` (`upload_file`) | 2 req/min | 5 |
| `pref/get` / `pref/set` | 60 req/min | 100 |

This server keeps **client-side token buckets matching these exact numbers**, so requests queue locally instead of getting rejected. It also retries the transient `LockFail` / `TooManyRequests` errors twice with backoff. Because of this, a search across a large workspace (`find_items` with no `file_ids`) can take a while — it must read every document within the 30/min limit. Pass `file_ids` when you know where to look.

### API error codes

Every response carries `_code` (`OK` on success) and `_msg`. This server raises a tool error containing both. Meanings:

| Code | Meaning |
|---|---|
| `InvalidToken` | API secret is invalid |
| `TooManyRequests` | Rate limit hit (server retries twice before surfacing this) |
| `Invalid` | Request body was not valid JSON / invalid fields |
| `LockFail` | Server couldn't process the request — transient, safe to retry |
| `Unauthorized` | No permission for that document |
| `NotFound` / `NodeNotFound` | Document / item does not exist |
| `NoInbox` | Inbox location not configured in your account |
| `NeedPro` | Feature requires a Pro account (file upload) |
| `QuotaExceeded` | Upload quota reached |

### Other limitations

- **No search endpoint.** `find_items` is client-side: it reads whole documents and matches locally. Expensive on large workspaces.
- **No delete at the file level.** `file/edit` supports only `create`, `edit` (rename), and `move` — you cannot delete documents or folders via the API.
- **Upload is Pro-only** (`NeedPro`) and heavily rate-limited (2/min).
- **Opaque IDs everywhere.** Documents, folders, and items are referenced only by random IDs; there are no slugs or URLs to parse. The outline renderer and `list_files` paths mitigate this.
- **Document edits are per-document.** A batch of `doc/edit` changes applies to one `file_id` only; editing N documents takes N requests.
- **`check_for_updates` silently drops** inaccessible/nonexistent document IDs rather than reporting them.
- **Version numbers are opaque integers.** Useful for change detection, but there is no history/diff/undo endpoint.
- **Permission levels are read-only metadata** (`0` none, `1` read, `2` edit, `3` manage, `4` owner); the API can't share or change permissions.
- **One token = one account, full access.** There are no scopes; the secret reads *and writes* everything the account can. Use a dedicated account if you want to limit blast radius.

## Security notes

- The token is read only from `DYNALIST_API_TOKEN`; the server never logs it and never sends it anywhere except `https://dynalist.io/api/v1/*` over HTTPS.
- Keep the token in your MCP client's `env` block or a git-ignored `.env` — never in committed files.

## Development

```bash
uv venv && uv pip install -e .
python -c "import asyncio, dynalist_mcp.server as s; print(len(asyncio.run(s.server.list_tools())), 'tools registered')"
```

## License

MIT — see [LICENSE](LICENSE).
