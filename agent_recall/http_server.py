"""MCP server over Streamable HTTP — for hosts that connect by URL.

Grok's Team Bot, among others, cannot hand secrets to a process it spawns, but
it can attach headers to a remote MCP server. So here every request carries its
caller's Recall key, and the server holds none:

    Authorization: Bearer <recall api key>     required
    X-Recall-Region: eu-central-1              optional, else $RECALL_REGION
    X-Recall-Bot-Name: Notetaker               optional, else $RECALL_BOT_NAME

The protocol is the same JSON-RPC ``mcp_server.respond`` answers over stdio;
only the I/O differs. One endpoint, ``POST /mcp``, takes a message and returns
the JSON response — or 202 with no body for a notification. The server is
stateless: it never issues an ``Mcp-Session-Id`` and offers no SSE stream, so
``GET`` and ``DELETE`` answer 405 as the spec allows.

Stdlib only. Run it as the installed console script::

    agent-recall-http                       # 127.0.0.1:8000, for this machine
    agent-recall-http --host 0.0.0.0        # or HOST / PORT in the environment

Serve it publicly only behind something that terminates TLS: the key travels
in a header, so plain HTTP off the machine would expose it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import recall_api as api
from .mcp_server import SERVER_NAME, respond

ENDPOINT = "/mcp"

# A JSON-RPC request to this server is a tool name and a few short arguments;
# anything near this size is not one, and reading it would only cost memory.
MAX_BODY_BYTES = 1024 * 1024

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


class McpHandler(BaseHTTPRequestHandler):
    # HTTP/1.1 so clients can reuse the connection between calls.
    protocol_version = "HTTP/1.1"
    server_version = SERVER_NAME

    # Bounds a client that opens a connection and never finishes the request.
    # It applies to socket reads only, not to the Recall call behind a tools/call.
    timeout = 30

    # The default log_message writes the request line only, never headers, so
    # the key cannot reach the logs. Keep it that way if this is ever changed.

    def _send(self, status: int, body: bytes = b"", content_type=None, headers=None):
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_json(self, status: int, payload: dict):
        # ensure_ascii for the same reason as stdio: the wire stays pure ASCII.
        self._send(status, json.dumps(payload, ensure_ascii=True).encode("ascii"),
                   "application/json")

    def _refuse(self, status: int, message: str, headers=None):
        # The request body has not been read, so it cannot be left on a kept-alive
        # connection to be parsed as the next request.
        self.close_connection = True
        self._send(status, message.encode() + b"\n", "text/plain; charset=utf-8", headers)

    def _bearer_key(self) -> str:
        scheme, _, key = (self.headers.get("Authorization") or "").strip().partition(" ")
        return key.strip() if scheme.lower() == "bearer" else ""

    def do_POST(self):
        if self.path.split("?", 1)[0] != ENDPOINT:
            self._refuse(HTTPStatus.NOT_FOUND, f"Not found. The MCP endpoint is {ENDPOINT}.")
            return

        # Every request must bring its own key. The server's environment never
        # stands in for a missing one, or any caller could spend the host's.
        key = self._bearer_key()
        if not key:
            self._refuse(HTTPStatus.UNAUTHORIZED,
                         "Send the Recall API key as 'Authorization: Bearer <key>'.",
                         {"WWW-Authenticate": "Bearer"})
            return

        # No Origin check: a DNS-rebinding page can reach this server but cannot
        # supply the key, which comes from an explicit header, never a cookie.

        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            self._refuse(HTTPStatus.LENGTH_REQUIRED, "Send a Content-Length; chunked bodies are not supported.")
            return
        try:
            length = int(self.headers.get("Content-Length") or "")
        except ValueError:
            self._refuse(HTTPStatus.LENGTH_REQUIRED, "Content-Length is required.")
            return
        if length < 0 or length > MAX_BODY_BYTES:
            self._refuse(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                         f"Request bodies are limited to {MAX_BODY_BYTES} bytes.")
            return

        body = self.rfile.read(length).decode("utf-8", errors="replace")

        with api.credentials(
            RECALL_API_KEY=key,
            RECALL_REGION=self.headers.get("X-Recall-Region"),
            RECALL_BOT_NAME=self.headers.get("X-Recall-Bot-Name"),
        ):
            response = respond(body)

        if response is None:
            # A notification, or a response to a request this server never sends.
            self._send(HTTPStatus.ACCEPTED)
        elif "error" in response and response.get("id") is None:
            # The body was not a usable message at all; the spec asks for an
            # HTTP error status, with the id-less JSON-RPC error as its body.
            self._send_json(HTTPStatus.BAD_REQUEST, response)
        else:
            self._send_json(HTTPStatus.OK, response)

    def _not_offered(self):
        if self.path.split("?", 1)[0] != ENDPOINT:
            self._refuse(HTTPStatus.NOT_FOUND, f"Not found. The MCP endpoint is {ENDPOINT}.")
            return
        # No server-initiated stream (GET) and no sessions to end (DELETE).
        self._refuse(HTTPStatus.METHOD_NOT_ALLOWED, "Only POST is supported.",
                     {"Allow": "POST"})

    do_GET = _not_offered
    do_DELETE = _not_offered


def build_server(host: str, port: int) -> ThreadingHTTPServer:
    """A threaded server: one slow Recall call must not stall other callers."""
    return ThreadingHTTPServer((host, port), McpHandler)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent-recall-http",
        description="Serve the Recall tools over MCP Streamable HTTP at POST /mcp.",
    )
    parser.add_argument("--host", default=os.environ.get("HOST") or DEFAULT_HOST,
                        help=f"Interface to bind (default $HOST or {DEFAULT_HOST})")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("PORT") or DEFAULT_PORT),
                        help=f"Port to bind (default $PORT or {DEFAULT_PORT})")
    args = parser.parse_args(argv)

    server = build_server(args.host, args.port)
    host, port = server.server_address[:2]
    print(f"{SERVER_NAME}: serving MCP at http://{host}:{port}{ENDPOINT}", file=sys.stderr)
    try:
        api.region()
    except api.RecallConfigError as err:
        print(f"{SERVER_NAME}: {err} Callers without an X-Recall-Region header "
              f"will get a configuration error.", file=sys.stderr)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
