# Contributing

Thanks for taking a look! Small, focused PRs are easiest to merge.

## Setup

```bash
git clone https://github.com/quartztester/dynalist-mcp.git
cd dynalist-mcp
uv venv && uv pip install -e .    # or: python -m venv .venv && pip install -e .
```

No Dynalist account or token is needed for development — the CI checks (and the
snippet below) run offline with no credentials.

## Checks CI runs

```bash
.venv/bin/python - <<'EOF'
import asyncio, dynalist_mcp.server as s
print(len(asyncio.run(s.server.list_tools())), "tools")
EOF
```

CI asserts the exact tool manifest and unit-tests the outline/path/validation
helpers. If you intentionally rename, add, or remove a tool, update the
`expected` set in `.github/workflows/ci.yml` in the same PR.

## Conventions

- Keep the client-side rate-limit buckets in `server.py` in sync with
  [the published API limits](https://apidocs.dynalist.io/) if Dynalist changes them,
  and mirror any change in the README's Limitations table.
- Validate change/action payloads locally with per-action field allow-lists so
  callers get a clear `ValueError` instead of an API round-trip.
- Never commit tokens or anything personal; auth is `DYNALIST_API_TOKEN` only.

## Ideas that don't need a proposal

Typos, doc gaps, clearer error messages, test coverage. For new tools or
behavior changes, open an issue first.
