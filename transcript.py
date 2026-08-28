"""Turning Recall's raw transcript JSON into something a human reads.

Recall returns a list of ``{participant, words[]}`` utterances. Consecutive
utterances from the same speaker are merged into paragraphs. Ported from
Recall's own reference implementation, ``convert_to_readable_transcript.ts``,
and checked against real transcript output.
"""

from __future__ import annotations

import json
from pathlib import Path

try:  # Inside a Hermes process this is the single source of truth.
    from hermes_constants import get_hermes_home
except ImportError:  # Standalone (tests, or the plugin imported directly).
    import os

    def get_hermes_home() -> Path:
        """Mirror hermes_constants' resolution: HERMES_HOME env, else default.

        Deliberately honours HERMES_HOME rather than hardcoding ~/.hermes —
        otherwise a test or a non-default profile silently writes transcripts
        into the wrong tree.
        """
        override = (os.environ.get("HERMES_HOME") or "").strip()
        return Path(override) if override else Path.home() / ".hermes"


def output_dir(bot_id: str) -> Path:
    """Where transcripts land: ``$HERMES_HOME/workspace/recall/<bot_id>/``."""
    return Path(get_hermes_home()) / "workspace" / "recall" / bot_id


def participant_key(participant: dict):
    """Identify a speaker by id, falling back to name when the id is absent."""
    participant_id = participant.get("id")
    return participant_id if participant_id is not None else participant.get("name")


def to_readable(parts):
    """
    Collapse Recall's per-utterance transcript into speaker-grouped paragraphs.

    Entries without a participant name or without words are skipped — they
    carry no readable content.
    """
    merged = []
    for entry in parts or []:
        participant = entry.get("participant") or {}
        words = entry.get("words") or []
        if not participant.get("name") or not words:
            continue

        if merged and participant_key(participant) == participant_key(merged[-1]["participant"]):
            merged[-1]["words"].extend(words)
            continue

        merged.append({"participant": participant, "words": list(words)})

    out = []
    for entry in merged:
        words = entry["words"]
        text = " ".join(w.get("text", "") for w in words).strip()
        if not text:
            continue

        first = next((w for w in words if w.get("start_timestamp")), None)
        last = next((w for w in reversed(words) if w.get("end_timestamp")), None)

        out.append({
            "speaker": entry["participant"]["name"],
            "start": (first or {}).get("start_timestamp", {}).get("relative"),
            "end": (last or {}).get("end_timestamp", {}).get("relative"),
            "text": text,
        })
    return out


def timecode(seconds) -> str:
    if seconds is None:
        return "--:--:--"
    seconds = int(seconds)
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def transcript_line(item: dict) -> str:
    return f"[{timecode(item['start'])}] {item['speaker']}: {item['text']}"


def as_text(readable) -> str:
    return "\n\n".join(transcript_line(item) for item in readable) + "\n"


def write_outputs(bot_id: str, raw_parts, readable):
    """Write both the readable transcript and the raw JSON. Returns their paths."""
    directory = output_dir(bot_id)
    directory.mkdir(parents=True, exist_ok=True)

    raw_path = directory / "transcript.json"
    text_path = directory / "transcript.txt"

    raw_path.write_text(json.dumps(raw_parts, indent=2, ensure_ascii=False))
    text_path.write_text(as_text(readable))

    return text_path, raw_path


def speakers(readable):
    return sorted({item["speaker"] for item in readable})
