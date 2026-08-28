"""Tool schemas — what the LLM reads to decide when to call each tool.

Plugin skills are not listed in the system prompt's skill index, so each
description points at ``recall:notetaker`` where the workflow actually matters.
"""

_SKILL_HINT = (
    "For the full workflow (scheduling lead time, waiting-room admission, "
    "how speaker labels work), load the skill 'recall:notetaker'."
)

SCHEDULE_BOT = {
    "name": "recall_schedule_bot",
    "description": (
        "Send a Recall.ai notetaker bot to a video meeting to record and later "
        "transcribe it. Works with Zoom, Google Meet and Microsoft Teams links. "
        "Returns a bot_id used by every other recall_* tool.\n\n"
        "Schedule more than 10 minutes ahead whenever you can: Recall reserves a "
        "machine for scheduled bots so they join on time, while last-minute "
        "('ad-hoc') bots come from a small pool that can be exhausted.\n\n"
        "This does NOT transcribe. After the meeting ends, call "
        "recall_get_transcript. " + _SKILL_HINT
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "meeting_url": {
                "type": "string",
                "description": (
                    "The real meeting URL (e.g. https://zoom.us/j/123?pwd=abc). "
                    "Redirect wrappers such as Google Calendar or SafeLinks are "
                    "not accepted — unwrap them to the underlying URL first."
                ),
            },
            "join_at": {
                "type": "string",
                "description": (
                    "When the bot should join. Either a relative offset "
                    "('+45m', '+2h', '+3d') or an ISO 8601 timestamp "
                    "('2026-08-29T14:00', local time if no timezone given). "
                    "Omit or pass 'now' to join immediately."
                ),
            },
        },
        "required": ["meeting_url"],
    },
}

BOT_STATUS = {
    "name": "recall_bot_status",
    "description": (
        "Check where a Recall bot is in its lifecycle. Returns immediately — it "
        "does not wait. Statuses progress: joining_call -> in_waiting_room -> "
        "in_call_recording -> call_ended -> done.\n\n"
        "'in_waiting_room' means a human must admit the bot from the meeting's "
        "participants panel or nothing will be recorded — surface that to the "
        "user rather than waiting. 'done' means the recording is ready and "
        "recall_get_transcript can be called."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "bot_id": {"type": "string", "description": "Bot id from recall_schedule_bot."},
        },
        "required": ["bot_id"],
    },
}

LIST_BOTS = {
    "name": "recall_list_bots",
    "description": (
        "List Recall bots: those scheduled for the future and those that have "
        "recently finished. Use this to recover a bot_id you no longer have, or "
        "to check what recordings are waiting to be transcribed."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

GET_TRANSCRIPT = {
    "name": "recall_get_transcript",
    "description": (
        "Fetch the speaker-labelled transcript for a finished meeting, starting "
        "transcription if it has not begun. Transcription is a separate job from "
        "recording and is NOT automatic.\n\n"
        "Returns immediately and never blocks. If the transcript is not ready it "
        "returns status 'processing' with retry_after_seconds — call again after "
        "roughly that long rather than waiting. When ready, returns status 'done' "
        "with the speakers, a preview, and the path to the saved transcript.\n\n"
        "Safe to call repeatedly: an existing transcript is reused rather than "
        "re-transcribed (which would cost money again)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "bot_id": {"type": "string", "description": "Bot id from recall_schedule_bot."},
            "preview_utterances": {
                "type": "integer",
                "description": (
                    "How many utterances to include inline in the result "
                    "(default 20). The full transcript is always written to disk."
                ),
            },
        },
        "required": ["bot_id"],
    },
}

CANCEL_BOT = {
    "name": "recall_cancel_bot",
    "description": (
        "Cancel a Recall bot. If it is scheduled more than 10 minutes out it is "
        "deleted outright; if it is closer to joining, or already in the call, it "
        "is removed from the call instead. Cancelling a finished bot does nothing. "
        "Use this when a meeting is called off, or a bot was scheduled by mistake "
        "— a bot left in a waiting room bills until it times out."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "bot_id": {"type": "string", "description": "Bot id to cancel."},
        },
        "required": ["bot_id"],
    },
}
