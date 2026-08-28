# hermes-recall-plugin

A [Hermes](https://github.com/NousResearch/hermes-agent) plugin that sends a
[Recall.ai](https://recall.ai) bot into a Zoom, Google Meet or Microsoft Teams
call to record it, then fetches a **speaker-labelled transcript** afterwards.

Explicit by design: the bot only joins meeting URLs you pass it. No calendar
scanning, no auto-dial.

## Install

```bash
hermes plugins install linusmimietz/hermes-recall-plugin
hermes plugins enable hermes-recall-plugin
```

You'll be prompted for `RECALL_API_KEY` and `RECALL_REGION` during install.

Or install it manually:

```bash
git clone https://github.com/linusmimietz/hermes-recall-plugin \
  ~/.hermes/plugins/hermes-recall-plugin
```

then add `hermes-recall-plugin` to `plugins.enabled` in `~/.hermes/config.yaml`.

## Configuration

All configuration is environment-driven — nothing is stored in this repo.

| Variable | Required | Description |
|---|---|---|
| `RECALL_API_KEY` | yes | Recall.ai API key. **Region-scoped.** |
| `RECALL_REGION` | yes | `us-east-1`, `us-west-2`, `eu-central-1` or `ap-northeast-1` |
| `RECALL_BOT_NAME` | no | Display name in the meeting. Default `Hermes Notetaker`. |

> **Region matters.** Keys, bots, recordings and transcripts all live in exactly
> one Recall region. A key used against the wrong region returns `401` with no
> explanation, which is why `RECALL_REGION` is required rather than guessed.
> Get your key from the dashboard for *your* region, e.g.
> `https://eu-central-1.recall.ai/dashboard/developers/api-keys`.

See `.env.example`.

## Tools

| Tool | What it does |
|---|---|
| `recall_schedule_bot` | Send a bot to a meeting, now or at a given time |
| `recall_bot_status` | Where the bot is in its lifecycle |
| `recall_list_bots` | Scheduled and recently finished bots |
| `recall_get_transcript` | Start transcription and fetch the result |
| `recall_cancel_bot` | Unschedule, or pull a bot out of a call |

Transcripts are written to `$HERMES_HOME/workspace/recall/<bot_id>/` as
`transcript.txt` (readable) and `transcript.json` (raw).

The plugin also bundles a skill, `recall:notetaker`, covering the workflow in
more depth. Load it with `skill_view("recall:notetaker")`.

## CLI

```bash
hermes recall schedule 'https://zoom.us/j/123?pwd=abc' +45m
hermes recall list
hermes recall status <bot_id>
hermes recall transcript <bot_id>
hermes recall cancel <bot_id>
```

## Three things that will bite you

**Schedule more than 10 minutes ahead.** Recall reserves a machine for scheduled
bots, so they join on time and are exempt from the 30-concurrent cap.
Last-minute ("ad-hoc") bots come from a small warm pool that can return `507`.

**Somebody has to admit the bot.** It joins as an unauthenticated guest, so if
the meeting has a waiting room a human must let it in — and Zoom never times a
bot out of a waiting room on its own. This is the usual reason a recording ends
up empty.

**Transcription is a separate job.** Recording and transcribing are two
different things; a finished recording has no transcript until one is requested.

## Design notes

**Every tool is one-shot.** None of them poll or block. Work that takes time
returns `status: "processing"` with a `retry_after_seconds` hint, and the agent
decides when to come back. A handler that blocks for minutes burns an agent turn
and risks a timeout.

**Speaker labels come from perfect diarization.** Each participant's audio
stream is transcribed independently, so utterances carry real participant names
rather than `Speaker A`. Adding a third-party provider's own diarization flag
would downgrade that, so the plugin doesn't.

**Stdlib only.** No third-party HTTP dependency, so installing this adds nothing
to your Hermes dependency tree.

**Polling, not webhooks.** Recall recommends webhooks for production. Hermes'
webhook signature validation currently reads only `svix-*` headers, while Recall
sends the Standard Webhooks `webhook-*` spelling — so deliveries fail
verification. Once Hermes accepts both spellings, a webhook-driven variant
becomes straightforward.

## License

MIT
