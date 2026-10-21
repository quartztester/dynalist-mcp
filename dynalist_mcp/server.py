"""Dynalist MCP server.

Exposes the Dynalist API v1 (https://apidocs.dynalist.io/) as MCP tools.
Auth: set the DYNALIST_API_TOKEN environment variable to your personal
API secret from https://dynalist.io/developer.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
from typing import Any

import httpx
from mcp.server.mcpserver import MCPServer

API_BASE = "https://dynalist.io/api/v1"

# Per-endpoint token buckets from the official docs: (sustained per-minute, burst).
_RATE_LIMITS: dict[str, tuple[float, int]] = {
    "file/list": (6, 10),
    "file/edit": (60, 50),
    "doc/read": (30, 100),
    "doc/check_for_updates": (60, 50),
    "doc/edit": (60, 20),
    "inbox/add": (60, 20),
    "upload": (2, 5),
    "pref/get": (60, 100),
    "pref/set": (60, 100),
}

# doc/edit has a second bucket for the number of changes per request.
_DOC_EDIT_CHANGE_BUDGET = (240, 500)  # (changes per minute, burst)


def _get_token() -> str:
    token = os.environ.get("DYNALIST_API_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "DYNALIST_API_TOKEN is not set. Get your API secret from "
            "https://dynalist.io/developer and set it in your MCP client config."
        )
    return token


class _TokenBucket:
    """Simple client-side token bucket so agents don't trip server 429s."""

    def __init__(self, per_minute: float, burst: int) -> None:
        self.rate = per_minute / 60.0  # tokens per second
        self.capacity = float(burst)
        self.tokens = float(burst)
        self.updated = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self, amount: float = 1.0, max_wait: float = 90.0) -> None:
        deadline = time.monotonic() + max_wait
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= amount:
                    self.tokens -= amount
                    return
                deficit = amount - self.tokens
            wait = min(deficit / self.rate, max(0.1, deadline - time.monotonic()))
            if time.monotonic() + wait > deadline:
                raise RuntimeError(
                    f"Rate limiter: waited {max_wait:.0f}s for capacity and still blocked. "
                    "Try fewer/smaller requests."
                )
            time.sleep(wait)


_buckets: dict[str, _TokenBucket] = {
    ep: _TokenBucket(rate, burst) for ep, (rate, burst) in _RATE_LIMITS.items()
}
_changes_bucket = _TokenBucket(*_DOC_EDIT_CHANGE_BUDGET)
_buckets_lock = threading.Lock()


def _call(endpoint: str, payload: dict[str, Any], n_changes: int = 0) -> dict[str, Any]:
    """POST to a Dynalist endpoint with throttling and error handling.

    Retries LockFail (server-side transient) and TooManyRequests (should be rare
    thanks to the client-side buckets) a couple of times with backoff.
    """
    payload = dict(payload)
    payload["token"] = _get_token()

    bucket = _buckets[endpoint]
    for attempt in range(3):
        bucket.acquire()
        if endpoint == "doc/edit" and n_changes > 0:
            _changes_bucket.acquire(amount=float(n_changes))
        try:
            resp = httpx.post(
                f"{API_BASE}/{endpoint}",
                json=payload,
                timeout=30.0,
                headers={"Content-Type": "application/json"},
            )
        except httpx.HTTPError as exc:
            if attempt == 2:
                raise RuntimeError(f"Network error calling {endpoint}: {exc}") from exc
            time.sleep(2.0 * (attempt + 1))
            continue

        try:
            data = resp.json()
        except ValueError:
            raise RuntimeError(
                f"{endpoint} returned non-JSON (HTTP {resp.status_code}): {resp.text[:200]}"
            )

        code = data.get("_code", "Unknown")
        if code == "OK":
            return data
        msg = data.get("_msg", "")
        if code in ("LockFail", "TooManyRequests") and attempt < 2:
            time.sleep(3.0 * (attempt + 1))
            continue
        raise RuntimeError(f"Dynalist API error [{code}] on {endpoint}: {msg}")
    raise RuntimeError(f"Dynalist API: exhausted retries on {endpoint}")


# ---------------------------------------------------------------------------
# Node-tree helpers
# ---------------------------------------------------------------------------

def _index_nodes(nodes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {n["id"]: n for n in nodes}


def _render_outline(nodes: list[dict[str, Any]], max_items: int = 2000) -> str:
    """Render the flat node list as an indented outline annotated with node IDs."""
    by_id = _index_nodes(nodes)
    child_ids = {cid for n in nodes for cid in n.get("children", [])}
    roots = [n for n in nodes if n["id"] not in child_ids]
    lines: list[str] = []

    def walk(node: dict[str, Any], depth: int) -> None:
        if len(lines) >= max_items:
            return
        flags = []
        if node.get("checkbox"):
            flags.append("x" if node.get("checked") else " ")
        if node.get("heading"):
            flags.append(f"H{node['heading']}")
        prefix = f"[{''.join(flags)}] " if flags else ""
        content = (node.get("content") or "").replace("\n", " ")
        note = f"  // note: {node['note']!r}" if node.get("note") else ""
        lines.append(f"{'  ' * depth}{prefix}{content}  <{node['id']}>{note}")
        for cid in node.get("children", []):
            child = by_id.get(cid)
            if child:
                walk(child, depth + 1)

    for root in roots:
        walk(root, 0)
    if len(lines) >= max_items:
        lines.append(f"... (truncated at {max_items} items)")
    return "\n".join(lines)


def _flatten_files(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return [{id, title, type, permission, path}] with folder paths resolved."""
    by_id = {f["id"]: f for f in files}
    out: list[dict[str, Any]] = []

    # file/list gives folders a `children` array; derive parent links from it.
    parents: dict[str, str] = {}
    for f in files:
        for cid in f.get("children", []):
            parents[cid] = f["id"]

    def path_of(fid: str, seen: set[str]) -> str:
        parts: list[str] = []
        cur: str | None = fid
        while cur and cur not in seen:
            seen.add(cur)
            f = by_id.get(cur)
            if not f:
                break
            parts.append(f.get("title") or "Untitled")
            cur = parents.get(cur)
        return "/" + "/".join(reversed(parts))

    for f in files:
        f = dict(f)
        f.pop("children", None)
        f["parent_id"] = parents.get(f["id"])
        f["path"] = path_of(f["id"], set())
        out.append(f)
    return out


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

server = MCPServer(
    "dynalist",
    instructions=(
        "Dynalist outliner integration. Documents contain a tree of items "
        "(nodes). Use dynalist_list_files to find document IDs, "
        "dynalist_read_document to see items and their <node IDs>, and pass "
        "those node IDs to dynalist_edit_document. Prefer dynalist_find_items "
        "over reading whole documents when you only need to locate items."
    ),
)


@server.tool()
def dynalist_list_files() -> str:
    """List all Dynalist documents and folders with IDs and full paths.

    Start here to discover file IDs for the other tools.
    """
    data = _call("file/list", {})
    flat = _flatten_files(data.get("files", []))
    return json.dumps(
        {"root_file_id": data.get("root_file_id"), "files": flat}, indent=2
    )


@server.tool()
def dynalist_read_document(file_id: str) -> str:
    """Read a document: metadata, a rendered outline with <node IDs>, and raw nodes.

    The outline shows the item tree with indentation and each item's node ID
    in angle brackets; use those IDs with dynalist_edit_document.
    """
    data = _call("doc/read", {"file_id": file_id})
    nodes = data.get("nodes", [])
    return json.dumps(
        {
            "file_id": data.get("file_id"),
            "title": data.get("title"),
            "version": data.get("version"),
            "outline": _render_outline(nodes),
            "nodes": nodes,
        },
        indent=2,
    )


@server.tool()
def dynalist_find_items(
    query: str, file_ids: list[str] | None = None, case_sensitive: bool = False
) -> str:
    """Search item text (and notes) for a substring; returns matches with node IDs.

    Searches one or more documents (all documents if file_ids is omitted).
    Note: reading many documents is rate-limited (30 reads/min) — pass
    file_ids when you know where to look.
    """
    if file_ids:
        ids = list(file_ids)
    else:
        listing = _call("file/list", {})
        ids = [f["id"] for f in listing.get("files", []) if f.get("type") == "document"]
    needle = query if case_sensitive else query.lower()
    matches: list[dict[str, Any]] = []
    by_file: dict[str, str] = {}
    for fid in ids:
        try:
            doc = _call("doc/read", {"file_id": fid})
        except RuntimeError as exc:
            # Skip documents we can't read (NotFound/Unauthorized) instead of failing all.
            if any(k in str(exc) for k in ("NotFound", "Unauthorized")):
                continue
            raise
        title = doc.get("title") or fid
        by_file[fid] = title
        for node in doc.get("nodes", []):
            hay = f"{node.get('content', '')}\n{node.get('note', '')}"
            if not case_sensitive:
                hay = hay.lower()
            if needle in hay:
                matches.append(
                    {
                        "file_id": fid,
                        "file_title": title,
                        "node_id": node["id"],
                        "content": node.get("content"),
                        "note": node.get("note") or None,
                        "checked": node.get("checked", False),
                    }
                )
    return json.dumps({"query": query, "count": len(matches), "matches": matches}, indent=2)


@server.tool()
def dynalist_edit_document(file_id: str, changes: list[dict[str, Any]]) -> str:
    """Apply a batch of node changes to one document.

    Each change is one of:
      insert: {action:"insert", parent_id, index, content, note?, checked?,
               checkbox?, heading? (1-3), color? (1-6)}
      edit:   {action:"edit", node_id, content?, note?, checked?, checkbox?,
               heading?, color?}
      move:   {action:"move", node_id, parent_id, index}
      delete: {action:"delete", node_id}
    index is zero-based among siblings; -1 = end, 0 = top.
    Returns per-change success booleans and new node IDs for inserts.
    """
    allowed = {
        "insert": {"action", "parent_id", "index", "content", "note", "checked", "checkbox", "heading", "color"},
        "edit": {"action", "node_id", "content", "note", "checked", "checkbox", "heading", "color"},
        "move": {"action", "node_id", "parent_id", "index"},
        "delete": {"action", "node_id"},
    }
    for i, ch in enumerate(changes):
        action = ch.get("action")
        if action not in allowed:
            raise ValueError(f"change[{i}]: action must be one of {sorted(allowed)}")
        unknown = set(ch) - allowed[action]
        if unknown:
            raise ValueError(f"change[{i}] ({action}): unknown fields {sorted(unknown)}")
    data = _call("doc/edit", {"file_id": file_id, "changes": changes}, n_changes=len(changes))
    return json.dumps(
        {"results": data.get("results"), "new_node_ids": data.get("new_node_ids")}, indent=2
    )


@server.tool()
def dynalist_edit_files(changes: list[dict[str, Any]]) -> str:
    """Create, rename, or move documents and folders.

    Each change is one of:
      create: {action:"create", type:"document"|"folder", parent_id, index, title?}
      edit:   {action:"edit", type, file_id, title}
      move:   {action:"move", type, file_id, parent_id, index}
    index is zero-based; -1 = end, 0 = top. Returns per-change booleans and
    created file IDs in request order.
    """
    allowed = {
        "create": {"action", "type", "parent_id", "index", "title"},
        "edit": {"action", "type", "file_id", "title"},
        "move": {"action", "type", "file_id", "parent_id", "index"},
    }
    for i, ch in enumerate(changes):
        action = ch.get("action")
        if action not in allowed:
            raise ValueError(f"change[{i}]: action must be one of {sorted(allowed)}")
        if ch.get("type") not in ("document", "folder"):
            raise ValueError(f"change[{i}]: type must be 'document' or 'folder'")
        unknown = set(ch) - allowed[action]
        if unknown:
            raise ValueError(f"change[{i}] ({action}): unknown fields {sorted(unknown)}")
    data = _call("file/edit", {"changes": changes})
    return json.dumps(
        {"results": data.get("results"), "created": data.get("created")}, indent=2
    )


@server.tool()
def dynalist_add_to_inbox(
    content: str,
    note: str | None = None,
    checked: bool = False,
    checkbox: bool = False,
    heading: int | None = None,
    color: int | None = None,
    index: int | None = None,
) -> str:
    """Send a new item to your Dynalist inbox (quick-capture).

    index: zero-based position in the inbox; omit to use your account setting.
    """
    payload: dict[str, Any] = {"content": content, "checked": checked, "checkbox": checkbox}
    if note is not None:
        payload["note"] = note
    if heading is not None:
        payload["heading"] = heading
    if color is not None:
        payload["color"] = color
    if index is not None:
        payload["index"] = index
    return json.dumps(_call("inbox/add", payload), indent=2)


@server.tool()
def dynalist_check_updates(file_ids: list[str]) -> str:
    """Return current version numbers for documents (cheap change detection).

    Inaccessible or missing documents are silently dropped from the response.
    Compare against versions seen in dynalist_read_document output.
    """
    data = _call("doc/check_for_updates", {"file_ids": file_ids})
    return json.dumps({"versions": data.get("versions")}, indent=2)


@server.tool()
def dynalist_upload_file(filename: str, content_type: str, data_base64: str) -> str:
    """Upload a base64-encoded file to Dynalist (Pro accounts only).

    Returns a dynalist.io/u/<id> URL for the uploaded file.
    """
    # Validate the payload locally for a clearer error than the API's.
    try:
        base64.b64decode(data_base64, validate=True)
    except Exception as exc:
        raise ValueError(f"data_base64 is not valid base64: {exc}")
    return json.dumps(
        _call("upload", {"filename": filename, "content_type": content_type, "data": data_base64}),
        indent=2,
    )


@server.tool()
def dynalist_get_preference(key: str) -> str:
    """Get an account preference. Known keys: inbox_location, inbox_move_position."""
    return json.dumps(_call("pref/get", {"key": key}), indent=2)


@server.tool()
def dynalist_set_preference(key: str, value: str) -> str:
    """Set an account preference (max 5MB value).

    inbox_location expects "<file ID>/<node ID>" or "<file ID>";
    inbox_move_position expects "top" or "bottom".
    """
    return json.dumps(_call("pref/set", {"key": key, "value": value}), indent=2)


def main() -> None:
    try:
        _get_token()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    server.run()  # stdio by default


if __name__ == "__main__":
    main()
