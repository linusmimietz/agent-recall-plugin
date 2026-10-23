"""Tool schemas — what the LLM reads to decide when to call each tool.

Plain ``{name, description, parameters}`` dicts, so they drop straight into any
function-calling API. The descriptions carry the operational rules (lead time,
waiting-room admission, transcription being a separate job) because a generic
host has nowhere else to put them; ``SKILL.md`` covers the same ground at
length for a system prompt.
"""

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
        "recall_get_transcript."
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
        "with the speakers and the transcript text itself, one '[hh:mm:ss] "
        "Speaker: text' paragraph per speaker turn.\n\n"
        "Long transcripts come in pages: if has_more is true, call again with "
        "offset set to next_offset until it is false.\n\n"
        "Safe to call repeatedly: an existing transcript is reused rather than "
        "re-transcribed (which would cost money again)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "bot_id": {"type": "string", "description": "Bot id from recall_schedule_bot."},
            "offset": {
                "type": "integer",
                "description": (
                    "Index of the first speaker turn to return (default 0). "
                    "Pass the previous result's next_offset to read on."
                ),
            },
            "limit": {
                "type": "integer",
                "description": (
                    "Most speaker turns to return in one result "
                    "(default 200, at most 1000)."
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
