"""Tool handlers.

Handler contract, per the Hermes plugin guide:
  * signature ``handler(args: dict, **kwargs) -> str``
  * always return a JSON string, for success and failure alike
  * never raise — catch everything and return an error object instead

Every handler here is **one-shot**: it performs a single round trip and returns.
None of them poll or sleep waiting for Recall to finish work. A tool that blocks
for minutes burns an agent turn and risks a timeout, so where the CLI prototype
looped, these return ``status: "processing"`` with a ``retry_after_seconds``
hint and let the agent decide when to come back.
"""

from __future__ import annotations

import functools
import json
from datetime import datetime, timedelta, timezone

from . import recall_api as api
from . import transcript as tx

# How long to suggest waiting before re-checking each kind of pending work.
RETRY_BOT_SECONDS = 30
RETRY_TRANSCRIPT_SECONDS = 15


def _ok(**fields) -> str:
    return json.dumps({"success": True, **fields}, ensure_ascii=False)


def _err(message: str, **fields) -> str:
    return json.dumps({"success": False, "error": message, **fields}, ensure_ascii=False)


def handler(func):
    """Enforce the never-raise half of the handler contract."""
    @functools.wraps(func)
    def wrapper(args, **kwargs):
        del kwargs
        try:
            return func(args or {})
        except api.RecallConfigError as err:
            return _err(str(err), kind="configuration")
        except api.RecallError as err:
            return _err(str(err), kind="api")
        except Exception as err:  # pragma: no cover — defensive
            return _err(f"Unexpected {type(err).__name__}: {err}", kind="internal")
    return wrapper


# --------------------------------------------------------------------------
# Scheduling
# --------------------------------------------------------------------------

@handler
def schedule_bot(args):
    meeting_url = (args.get("meeting_url") or "").strip()
    if not meeting_url:
        return _err("meeting_url is required")

    join_at = api.parse_when(args.get("join_at"))
    lead_seconds = api.seconds_until(join_at)
    scheduled = lead_seconds > api.SCHEDULED_THRESHOLD_SECONDS

    if scheduled:
        # Bots take a moment to boot, so aim slightly early.
        join_at -= timedelta(seconds=api.JOIN_LEAD_SECONDS)

    name = api.bot_name()
    body = {
        "meeting_url": meeting_url,
        "join_at": api.iso(join_at),
        "bot_name": name,
        # Recall clears meeting_url a few days after the call and drops the
        # recording config after two weeks, but metadata is kept. Stashing the
        # URL here keeps listings readable for bots revisited much later.
        "metadata": {
            "meeting_url": meeting_url,
            "scheduled_by": "hermes-recall-plugin",
            "scheduled_at": api.iso(datetime.now(timezone.utc)),
        },
        "automatic_leave": {
            # Zoom waiting rooms never time out on the platform side, so this is
            # the only thing stopping a bot sitting there indefinitely.
            "waiting_room_timeout": 900,
            "noone_joined_timeout": 900,
        },
    }

    bot = api.request("POST", "/bot/", body)
    bot_id = bot["id"]

    result = {
        "bot_id": bot_id,
        "bot_name": name,
        "meeting_url": meeting_url,
        "join_at": api.iso(join_at),
        "joins_in": api.format_duration(lead_seconds),
        "mode": "scheduled" if scheduled else "ad-hoc",
        "next_step": (
            "Someone may need to admit the bot from the meeting's waiting room. "
            "After the meeting ends, call recall_get_transcript with this bot_id."
        ),
    }
    if not scheduled:
        result["warning"] = (
            "Booked less than 10 minutes ahead, so this is an ad-hoc bot: it "
            "comes from a limited pool of warm machines, is capped at 30 "
            "concurrent, and is not guaranteed to join on time. Schedule "
            "further ahead when the meeting time is known in advance."
        )
    return _ok(**result)


# --------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------

_STATUS_MEANING = {
    "joining_call": "Connecting to the meeting.",
    "in_waiting_room": (
        "Waiting to be admitted. A human must admit the bot from the meeting's "
        "participants panel, or it will give up after 15 minutes and record nothing."
    ),
    "in_call_not_recording": "In the meeting but not recording yet.",
    "in_call_recording": "Recording.",
    "recording_permission_denied": "The host denied the bot's request to record.",
    "call_ended": "The call ended. Media may still be uploading.",
    "done": "Finished — the recording is ready to transcribe.",
    "fatal": "The bot failed and could not continue.",
}


@handler
def bot_status(args):
    bot_id = (args.get("bot_id") or "").strip()
    if not bot_id:
        return _err("bot_id is required")

    bot = api.request("GET", f"/bot/{bot_id}/")
    status = api.latest_status(bot)
    code = status.get("code")
    recording = api.first_recording(bot)

    result = {
        "bot_id": bot_id,
        "status": code or "scheduled",
        "meaning": _STATUS_MEANING.get(code, "Scheduled; has not started joining yet."),
        "meeting": api.describe_meeting(bot),
        "has_recording": recording is not None,
        "finished": code in api.TERMINAL_BOT_STATUSES,
    }
    if status.get("sub_code"):
        result["sub_code"] = status["sub_code"]

    if not api.is_dispatched(bot) and bot.get("join_at"):
        join_at = api.parse_iso(bot["join_at"])
        result["join_at"] = api.iso(join_at)
        result["joins_in"] = api.format_duration(api.seconds_until(join_at))

    if code == "in_waiting_room":
        result["action_required"] = (
            f"Admit '{bot.get('bot_name') or api.bot_name()}' from the meeting's "
            f"participants panel."
        )
    elif code == "fatal":
        result["action_required"] = (
            f"Bot failed ({status.get('sub_code') or 'no sub_code'}). Inspect it at "
            f"https://{api.region()}.recall.ai/dashboard/explorer/bot/{bot_id}"
        )
    elif code == "done":
        result["next_step"] = (
            "Recording ready. Call recall_get_transcript with this bot_id."
            if recording else
            "The bot finished without producing a recording — it most likely was "
            "never admitted to the meeting."
        )
    elif not result["finished"]:
        result["retry_after_seconds"] = RETRY_BOT_SECONDS

    return _ok(**result)


# --------------------------------------------------------------------------
# Listing
# --------------------------------------------------------------------------

@handler
def list_bots(args):
    del args
    now = api.iso(datetime.now(timezone.utc))

    upcoming = (api.request("GET", f"/bot/?join_at_after={now}") or {}).get("results", [])
    scheduled = []
    for bot in sorted(upcoming, key=lambda b: b.get("join_at") or ""):
        join_at = api.parse_iso(bot["join_at"])
        scheduled.append({
            "bot_id": bot["id"],
            "meeting": api.describe_meeting(bot),
            "join_at": api.iso(join_at),
            "joins_in": api.format_duration(api.seconds_until(join_at)),
        })

    recent_raw = (api.request("GET", "/bot/?limit=10") or {}).get("results", [])
    recent = []
    for bot in recent_raw:
        if not api.is_dispatched(bot):
            continue
        recording = api.first_recording(bot)
        recent.append({
            "bot_id": bot["id"],
            "meeting": api.describe_meeting(bot),
            "status": api.latest_status(bot).get("code", "unknown"),
            "has_recording": recording is not None,
        })

    return _ok(scheduled=scheduled, recent=recent,
               scheduled_count=len(scheduled), recent_count=len(recent))


# --------------------------------------------------------------------------
# Transcript — one-shot, never blocks
# --------------------------------------------------------------------------

@handler
def get_transcript(args):
    bot_id = (args.get("bot_id") or "").strip()
    if not bot_id:
        return _err("bot_id is required")

    try:
        preview_count = int(args.get("preview_utterances") or 20)
    except (TypeError, ValueError):
        preview_count = 20
    preview_count = max(0, min(preview_count, 200))

    bot = api.request("GET", f"/bot/{bot_id}/")
    status = api.latest_status(bot).get("code")
    recording = api.first_recording(bot)

    if recording is None:
        if status in api.TERMINAL_BOT_STATUSES:
            return _err(
                "This bot produced no recording — it most likely was never "
                "admitted to the meeting.",
                bot_id=bot_id, status=status)
        return _ok(status="processing", bot_id=bot_id, bot_status=status or "scheduled",
                   retry_after_seconds=RETRY_BOT_SECONDS,
                   detail="The meeting has not finished recording yet.")

    # Reuse an existing transcript rather than paying to transcribe twice.
    existing = (recording.get("media_shortcuts") or {}).get("transcript") or {}
    url = (existing.get("data") or {}).get("download_url")
    transcript_id = existing.get("id")

    if not url:
        if transcript_id:
            # A job exists but has not finished; re-read it for a fresh URL.
            current = api.request("GET", f"/transcript/{transcript_id}/")
            code = (current.get("status") or {}).get("code")
            url = (current.get("data") or {}).get("download_url")
            if code == "failed":
                return _err("Transcription failed.", bot_id=bot_id,
                            transcript_id=transcript_id,
                            detail=current.get("status"))
            if not url:
                return _ok(status="processing", bot_id=bot_id,
                           transcript_id=transcript_id, transcript_status=code,
                           retry_after_seconds=RETRY_TRANSCRIPT_SECONDS)
        else:
            if status not in api.TERMINAL_BOT_STATUSES:
                return _ok(status="processing", bot_id=bot_id,
                           bot_status=status,
                           retry_after_seconds=RETRY_BOT_SECONDS,
                           detail="Waiting for the recording to finish before "
                                  "transcription can start.")
            started = _start_transcription(recording["id"])
            return _ok(status="processing", bot_id=bot_id,
                       transcript_id=started,
                       retry_after_seconds=RETRY_TRANSCRIPT_SECONDS,
                       detail="Transcription started. Call again shortly.")

    parts = api.download(url)
    readable = tx.to_readable(parts)
    if not readable:
        return _ok(status="done", bot_id=bot_id, utterances=0, speakers=[],
                   detail="The transcript is empty — nobody spoke, or no audio "
                          "was captured.")

    text_path, raw_path = tx.write_outputs(bot_id, parts, readable)
    names = tx.speakers(readable)

    return _ok(
        status="done",
        bot_id=bot_id,
        transcript_id=transcript_id,
        utterances=len(readable),
        speakers=names,
        transcript_path=str(text_path),
        raw_path=str(raw_path),
        preview=[tx.transcript_line(item) for item in readable[:preview_count]],
        truncated=len(readable) > preview_count,
    )


def _start_transcription(recording_id: str) -> str:
    """
    Kick off post-meeting transcription for a finished recording.

    ``use_separate_streams_when_available`` turns on Recall's perfect
    diarization: each participant's audio stream is transcribed independently,
    so utterances come back tagged with real participant names instead of
    generic "Speaker A" labels. Do not add a third-party provider's own diarize
    flag — that downgrades the result to generic labels.
    """
    body = {
        "provider": {"recallai_async": {"language_code": "auto"}},
        "diarization": {"use_separate_streams_when_available": True},
    }
    created = api.request("POST", f"/recording/{recording_id}/create_transcript/", body)
    return created["id"]


# --------------------------------------------------------------------------
# Cancellation
# --------------------------------------------------------------------------

@handler
def cancel_bot(args):
    bot_id = (args.get("bot_id") or "").strip()
    if not bot_id:
        return _err("bot_id is required")

    bot = api.request("GET", f"/bot/{bot_id}/")

    # A scheduled bot Recall has not reserved yet can be deleted outright.
    if not api.is_dispatched(bot) and bot.get("join_at"):
        if api.seconds_until(api.parse_iso(bot["join_at"])) > api.SCHEDULED_THRESHOLD_SECONDS:
            api.request("DELETE", f"/bot/{bot_id}/")
            return _ok(bot_id=bot_id, action="deleted",
                       detail="Scheduled bot deleted; it will not join.")

    status = api.latest_status(bot).get("code")
    if status in api.TERMINAL_BOT_STATUSES:
        return _ok(bot_id=bot_id, action="none", status=status,
                   detail="Bot already finished; nothing to cancel.")

    api.request("POST", f"/bot/{bot_id}/leave_call/")
    return _ok(bot_id=bot_id, action="removed_from_call",
               detail="Bot removed from the call.")
