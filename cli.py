"""``hermes recall`` — the same operations as the tools, from a terminal.

Useful for testing the plugin without going through the agent, and for one-off
scheduling. Mirrors ``plugins/google_meet/cli.py``: ``register_cli`` builds the
argparse tree, ``recall_command`` dispatches and returns an exit code.
"""

from __future__ import annotations

import argparse
import json

from . import recall_api as api
from . import tools


def register_cli(subparser: argparse.ArgumentParser) -> None:
    """Build the ``hermes recall`` argparse tree."""
    subs = subparser.add_subparsers(dest="recall_command")

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
        "--preview", type=int, default=20,
        help="Utterances to print inline (default 20)",
    )

    cancel = subs.add_parser("cancel", help="Unschedule a bot or pull it from the call")
    cancel.add_argument("bot_id")


def _emit(raw: str) -> int:
    """Pretty-print a handler's JSON result; exit non-zero on failure."""
    payload = json.loads(raw)
    if not payload.get("success"):
        print(f"error: {payload.get('error')}")
        return 1

    preview = payload.pop("preview", None)
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
    if preview:
        print()
        for line in preview:
            print(line)
    return 0


def recall_command(args: argparse.Namespace) -> int:
    sub = getattr(args, "recall_command", None)
    if not sub:
        print("usage: hermes recall {schedule,list,status,transcript,cancel}")
        return 2

    try:
        api.region()
        api.api_key()
    except api.RecallConfigError as err:
        print(f"error: {err}")
        return 2

    if sub == "schedule":
        return _emit(tools.schedule_bot(
            {"meeting_url": args.meeting_url, "join_at": args.when}))
    if sub == "list":
        return _emit(tools.list_bots({}))
    if sub == "status":
        return _emit(tools.bot_status({"bot_id": args.bot_id}))
    if sub == "transcript":
        return _emit(tools.get_transcript(
            {"bot_id": args.bot_id, "preview_utterances": args.preview}))
    if sub == "cancel":
        return _emit(tools.cancel_bot({"bot_id": args.bot_id}))

    print(f"unknown subcommand: {sub}")
    return 2
