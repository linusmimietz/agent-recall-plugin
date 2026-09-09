---
name: recall
description: Record and transcribe a meeting with Recall.ai — scheduling lead time, waiting-room admission, and how speaker labels are produced.
when-to-use: When the user wants a meeting recorded, transcribed or summarised from a Zoom, Google Meet or Microsoft Teams link, or asks about a notetaker bot already scheduled or sitting in a call.
metadata:
  author: Linus Mimietz
  short-description: Record and transcribe meetings with Recall.ai
---

# Recording a meeting with Recall.ai

A bot joins the call as a participant, records it, and afterwards a separate job
turns that recording into a speaker-labelled transcript.

```
recall_schedule_bot   →  bot joins, records
recall_bot_status     →  where is it? (one-shot, never blocks)
recall_get_transcript →  starts transcription, then returns it
```

## Schedule more than 10 minutes ahead

This is the single most important habit. The 10-minute mark is a real boundary:

| | Scheduled (>10 min out) | Ad-hoc (sooner) |
|---|---|---|
| Machine | Reserved for the bot | Taken from a small warm pool |
| Joins on time | Guaranteed | Usually — the pool can run dry |
| Failure mode | — | HTTP 507 |
| Concurrency cap | None | 30 per workspace |

If the user knows the meeting time, pass it as `join_at` (`"+45m"`,
`"2026-08-29T14:00"`). Only omit `join_at` for a meeting happening right now.

## Somebody has to let the bot in

Most meetings have a waiting room, and the bot joins as an unauthenticated
guest. When `recall_bot_status` returns `in_waiting_room`, **tell the user to
admit the bot** — do not sit and re-poll silently. Nothing is recorded until
someone clicks admit, and the bot gives up after 15 minutes.

This is the most common reason a recording ends up empty.

## Transcription is a separate step

Recording and transcription are two different jobs. A finished recording has no
transcript until `recall_get_transcript` starts one. It is not automatic.

`recall_get_transcript` is one-shot and never blocks. It returns either:

- `status: "processing"` with `retry_after_seconds` — the meeting is still
  running, or transcription is still working. Come back after roughly that long.
  Do not spin on it; if the meeting is still in progress, tell the user and stop.
- `status: "done"` — with the speakers, a preview, and a path on disk.

Calling it repeatedly is safe: an existing transcript is reused rather than
re-transcribed. Re-transcribing costs money.

## Speaker labels

Transcripts come back tagged with **real participant names**, because each
participant's audio stream is transcribed independently ("perfect diarization").

Two things degrade this to anonymous `Speaker A` / `Speaker B` labels:

- Enabling a third-party transcription provider's own diarization flag. This
  plugin deliberately does not.
- Several people sharing one microphone in a conference room — they collapse
  into whoever owns that audio stream.

## Things worth telling the user

- **Nothing deduplicates bots.** Scheduling twice for one meeting puts two bots
  in the room and bills for both. Check `recall_list_bots` before scheduling a
  meeting that may already be covered.
- **A cancelled meeting means a cancelled bot.** `recall_cancel_bot` — a bot
  left in a waiting room bills until it times out.
- **Recording people has consequences.** The bot is visible in the participant
  list, but that is not the same as consent. In many jurisdictions — and for
  employee recordings in particular — participants need to be told. Raise it
  rather than assuming it has been handled.
- **Meeting links must be real.** Google Calendar and SafeLinks wrappers are
  rejected; unwrap to the underlying `zoom.us` / `meet.google.com` /
  `teams.microsoft.com` URL first.

## Recovering a lost bot_id

`recall_list_bots` shows scheduled and recently finished bots with their
meetings. Use it rather than asking the user to dig out an id.
