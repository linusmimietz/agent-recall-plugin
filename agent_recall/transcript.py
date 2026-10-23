"""Turning Recall's raw transcript JSON into something a human reads.

Recall returns a list of ``{participant, words[]}`` utterances. Consecutive
utterances from the same speaker are merged into paragraphs. Ported from
Recall's own reference implementation, ``convert_to_readable_transcript.ts``,
and checked against real transcript output.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# Transcripts are returned inline, so writing them to disk is opt-in: a server
# shared by many callers must not keep each one's meetings on its own disk. A
# local agent that wants files sets RECALL_OUTPUT_DIR to its workspace.


def base_dir():
    """The transcript root, ``$RECALL_OUTPUT_DIR``, or None when it is unset."""
    configured = (os.environ.get("RECALL_OUTPUT_DIR") or "").strip()
    return Path(configured).expanduser() if configured else None


def _safe_segment(bot_id: str) -> str:
    """Reject anything that would not stay inside the output directory.

    Callers validate bot ids before reaching here; this is the guard at the
    filesystem boundary itself, so a path such as ``../../x`` cannot escape even
    if a caller forgets.
    """
    segment = str(bot_id or "").strip()
    if (not segment or segment.startswith(".")
            or "/" in segment or "\\" in segment or "\x00" in segment):
        raise ValueError(f"Unsafe transcript directory name: {bot_id!r}")
    return segment


def output_dir(bot_id: str):
    """Where transcripts land: ``$RECALL_OUTPUT_DIR/<bot_id>/``, or None."""
    base = base_dir()
    return base / _safe_segment(bot_id) if base is not None else None


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


def _write_atomic(path: Path, text: str) -> None:
    """Write UTF-8 through a temp file, then rename over the target.

    Both halves matter. Without an explicit encoding, Path.write_text uses the
    locale's codec, so a participant named 山田 or a curly apostrophe raises
    UnicodeEncodeError on a cp1252 or latin-1 system. And because the open()
    truncates before the encode fails, writing in place would destroy a good
    transcript from a previous run — after the user has already paid Recall to
    generate it. The rename is atomic, so the previous file survives a failure.
    """
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write_outputs(bot_id: str, raw_parts, readable):
    """Write both the readable transcript and the raw JSON. Returns their paths.

    Returns None, writing nothing, when ``RECALL_OUTPUT_DIR`` is unset.
    """
    directory = output_dir(bot_id)
    if directory is None:
        return None
    directory.mkdir(parents=True, exist_ok=True)

    raw_path = directory / "transcript.json"
    text_path = directory / "transcript.txt"

    _write_atomic(raw_path, json.dumps(raw_parts, indent=2, ensure_ascii=False))
    _write_atomic(text_path, as_text(readable))

    return text_path, raw_path


def speakers(readable):
    return sorted({item["speaker"] for item in readable})
