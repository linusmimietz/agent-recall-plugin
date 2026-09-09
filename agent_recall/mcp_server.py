"""MCP server over stdio — how Grok, Claude Code and Cursor get these tools.

Those hosts do not import Python plugins; they spawn an MCP server and speak
JSON-RPC 2.0 to it. This module is that server, and it is deliberately thin: the
tool table and handlers in this package are the implementation, and everything
here is protocol.

Stdlib only, like the rest of the package — the MCP stdio transport is
newline-delimited JSON-RPC over stdin/stdout, which needs no dependency. That
also means **stdout belongs to the protocol**: diagnostics go to stderr, and the
package logs through ``logging`` (stderr by default) rather than printing.

Run it as the installed console script::

    agent-recall-mcp

``python3 -m agent_recall.mcp_server`` also works, but prefer the script when a
host spawns it: ``-m`` puts the process working directory on ``sys.path`` ahead
of the stdlib, so a stray ``json.py`` in the user's project would be imported
instead of the real module. ``python3 -P -m ...`` is the safe equivalent.

Tool failures are returned as results with ``isError``, not as JSON-RPC errors,
so the model sees the error text and can correct itself. A JSON-RPC error means
the *protocol* was misused — an unknown tool name included.
"""

from __future__ import annotations

import json
import sys

from . import HANDLERS, TOOLS, __version__, dispatch, is_configured

SERVER_NAME = "agent-recall"

# Newest first: the version echoed back when a client asks for one we don't
# know. Nothing in this server is version-specific — it implements the subset
# (initialize, ping, tools/list, tools/call) stable across all three.
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")


# --------------------------------------------------------------------------
# JSON-RPC plumbing
# --------------------------------------------------------------------------

class UnknownTool(Exception):
    """A tools/call named something this server does not expose."""


def _result(msg_id, payload) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": payload}


def _error(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _write(message: dict) -> None:
    # ensure_ascii (the default, passed explicitly because it is load-bearing)
    # keeps the wire pure ASCII, so a non-UTF-8 stdout cannot corrupt a
    # transcript containing accented names.
    sys.stdout.write(json.dumps(message, ensure_ascii=True) + "\n")
    sys.stdout.flush()


# --------------------------------------------------------------------------
# MCP methods
# --------------------------------------------------------------------------

def tool_descriptors() -> list:
    """The package's schemas in MCP's shape: ``parameters`` -> ``inputSchema``."""
    return [
        {
            "name": tool["name"],
            "description": tool["description"],
            "inputSchema": tool["parameters"],
        }
        for tool in TOOLS
    ]


def _call_tool(params: dict) -> dict:
    """Run a tool. Raises UnknownTool so the caller can answer -32602."""
    name = params.get("name")
    if not isinstance(name, str) or name not in HANDLERS:
        raise UnknownTool(f"Unknown tool: {name}")

    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        arguments = {}

    raw = dispatch(name, arguments)

    # dispatch never raises and always returns the {"success": ...} envelope,
    # so this only guards against a handler being changed out from under us.
    try:
        failed = json.loads(raw).get("success") is False
    except (json.JSONDecodeError, AttributeError):
        failed = False

    return {"content": [{"type": "text", "text": raw}], "isError": failed}


def handle(message: dict):
    """Answer one JSON-RPC message. Returns None for notifications."""
    method = message.get("method") or ""
    params = message.get("params") or {}
    if not isinstance(params, dict):
        params = {}

    # No id at all means a notification: acknowledge nothing, answer nothing.
    # That includes notifications/cancelled — there is no reply to give, and the
    # request budget in recall_api is what bounds a call the client gave up on.
    if "id" not in message:
        return None

    msg_id = message["id"]
    if msg_id is None or not isinstance(msg_id, (str, int)) or isinstance(msg_id, bool):
        return _error(None, -32600, "Invalid Request: id must be a string or number")

    if method == "initialize":
        requested = params.get("protocolVersion")
        return _result(msg_id, {
            "protocolVersion": (
                requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            ),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": __version__},
        })

    if method == "ping":
        return _result(msg_id, {})

    if method == "tools/list":
        return _result(msg_id, {"tools": tool_descriptors()})

    if method == "tools/call":
        try:
            return _result(msg_id, _call_tool(params))
        except UnknownTool as err:
            # The spec reserves isError for execution failures; an unknown name
            # is the client calling something that was never advertised.
            return _error(msg_id, -32602, str(err))

    return _error(msg_id, -32601, f"Method not found: {method}")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    del argv  # no options: the host controls everything through the protocol

    if not is_configured():
        print(
            f"{SERVER_NAME}: RECALL_API_KEY / RECALL_REGION are missing or invalid. "
            f"The {len(HANDLERS)} tools are still advertised, but each call will "
            f"return a configuration error until the environment is fixed.",
            file=sys.stderr,
        )

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            _write(_error(None, -32700, "Parse error"))
            continue

        if not isinstance(message, dict):
            # Batches were removed from the MCP spec; one object per line.
            _write(_error(None, -32600, "Invalid Request: expected a single object"))
            continue

        try:
            response = handle(message)
        except Exception as err:  # pragma: no cover — a crash would kill the server
            _write(_error(message.get("id"), -32603,
                          f"Internal error: {type(err).__name__}: {err}"))
            continue

        if response is not None:
            _write(response)

    return 0


if __name__ == "__main__":
    sys.exit(main())
