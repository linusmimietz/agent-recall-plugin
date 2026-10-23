"""``agent-recall`` — the same operations as the tools, from a terminal.

Useful for testing without going through an agent, and for one-off scheduling.
``build_parser`` owns the whole argparse tree; ``main`` dispatches and returns
an exit code, so it works both as a console script and via
``python -m agent_recall``.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import recall_api as api
from . import tools

PROG = "agent-recall"


def build_parser() -> argparse.ArgumentParser:
    """Build the ``agent-recall`` argparse tree."""
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Send a Recall.ai bot to a meeting and fetch its transcript.",
    )
    subs = parser.add_subparsers(dest="command")

    schedule = subs.add_parser("schedule", help="Send a bot to a meeting")
    schedule.add_argument("meeting_url", help="Zoom / Meet / Teams URL")
    schedule.add_argument(
        "when", nargs="?", default=None,
        help="When to join: '+45m', '2026-08-29T14:00', or omit for now",
    )

    subs.add_parser("list", help="List scheduled and recent bots")

    status = subs.add_parser("status", help="Check a bot's lifecycle status")
    status.add_argument("bot_id")

    transcript = subs.add_parser("transcript", help="Fetch a bot's transcript")
    transcript.add_argument("bot_id")
    transcript.add_argument(
        "--offset", type=int, default=0,
        help="First speaker turn to print (default 0)",
    )
    transcript.add_argument(
        "--limit", type=int, default=None,
        help="Most speaker turns to print (default 200, at most 1000)",
    )

    cancel = subs.add_parser("cancel", help="Unschedule a bot or pull it from the call")
    cancel.add_argument("bot_id")

    return parser


def _emit(raw: str) -> int:
    """Pretty-print a handler's JSON result; exit non-zero on failure."""
    payload = json.loads(raw)
    if not payload.get("success"):
        print(f"error: {payload.get('error')}")
        return 1

    text = payload.pop("transcript", None)
    payload.pop("success", None)
    for key, value in payload.items():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            print(f"{key}:")
            for item in value:
                print("  " + "  ".join(f"{k}={v}" for k, v in item.items()))
        elif isinstance(value, list):
            print(f"{key}: {', '.join(str(v) for v in value)}")
        else:
            print(f"{key}: {value}")
    if text:
        print()
        print(text)
    return 0


def main(argv=None) -> int:
    # Transcripts carry names and punctuation the locale codec may not cover;
    # replacing an unencodable character beats aborting the whole command.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_usage()
        return 2

    try:
        api.region()
        api.api_key()
    except api.RecallConfigError as err:
        print(f"error: {err}")
        return 2

    if args.command == "schedule":
        return _emit(tools.schedule_bot(
            {"meeting_url": args.meeting_url, "join_at": args.when}))
    if args.command == "list":
        return _emit(tools.list_bots({}))
    if args.command == "status":
        return _emit(tools.bot_status({"bot_id": args.bot_id}))
    if args.command == "transcript":
        return _emit(tools.get_transcript(
            {"bot_id": args.bot_id, "offset": args.offset, "limit": args.limit}))
    if args.command == "cancel":
        return _emit(tools.cancel_bot({"bot_id": args.bot_id}))

    parser.print_usage()
    return 2
