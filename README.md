# agent-recall-plugin

Meeting-notetaker tools for any agent. Sends a [Recall.ai](https://recall.ai)
bot into a Zoom, Google Meet or Microsoft Teams call to record it, then fetches
a **speaker-labelled transcript** afterwards.

Stdlib-only Python. Plugs into [Grok](https://docs.x.ai/build/features/skills-plugins-marketplaces),
Claude Code and Cursor as an **MCP server**, or drops into your own agent loop as
a tool table and a dispatcher.

Explicit by design: the bot only joins meeting URLs you pass it. No calendar
scanning, no auto-dial.

## Install

### Grok (and Claude Code, Cursor)

These hosts don't load Python plugins — they spawn an MCP server and talk
JSON-RPC to it. This repo ships one, plus the manifests that wire it up.

**As a plugin** (also installs the `notetaker` skill):

```bash
grok plugin marketplace add linusmimietz/agent-recall-plugin
grok plugin install agent-recall@linusmimietz-plugins
```

Grok reads Claude Code plugin manifests, so the same repo works with
`/plugin marketplace add linusmimietz/agent-recall-plugin` in Claude Code.

The plugin declares `RECALL_API_KEY` and `RECALL_REGION` as `userConfig`, so a
host that prompts for plugin configuration (Claude Code does) asks for them at
install time and passes them through. Grok ignores `userConfig`, so there export
them in the shell that launches it — the spawned server inherits that
environment. Four files do the wiring, if you would rather set it up by hand:

| File | Role |
|---|---|
| `.mcp.json` | Declares the `recall` stdio server |
| `.claude-plugin/plugin.json` | Plugin manifest; points at `.mcp.json`, prompts for credentials |
| `.claude-plugin/marketplace.json` | Makes the repo installable as a marketplace |
| `skills/notetaker/SKILL.md` | The bundled skill |

Both routes run the `agent-recall-mcp` console script rather than
`python3 -m agent_recall.mcp_server`. That is deliberate: `-m` puts the spawning
process's working directory on `sys.path` ahead of the stdlib, so a stray
`json.py` in whatever project you happened to open would be imported instead of
the real module.

**Or just the MCP server**, if you don't want the plugin machinery:

```bash
pip install git+https://github.com/linusmimietz/agent-recall-plugin
grok mcp add recall -- agent-recall-mcp
```

That writes into `~/.grok/config.toml`; add `--scope project` to put it in
`.grok/config.toml` instead. By hand, it is:

```toml
[mcp_servers.recall]
command = "agent-recall-mcp"
env = { RECALL_API_KEY = "${RECALL_API_KEY}", RECALL_REGION = "us-east-1" }
```

Check it with `grok mcp doctor recall`, then `/mcps` in the TUI to see the five
tools.

### As a Python library

```bash
pip install git+https://github.com/linusmimietz/agent-recall-plugin
```

Or from a clone, for development:

```bash
git clone https://github.com/linusmimietz/agent-recall-plugin
cd agent-recall-plugin
pip install -e .
```

## Configuration

All configuration is environment-driven — nothing is stored in this repo.

| Variable | Required | Description |
|---|---|---|
| `RECALL_API_KEY` | yes | Recall.ai API key. **Region-scoped.** |
| `RECALL_REGION` | yes | `us-east-1`, `us-west-2`, `eu-central-1` or `ap-northeast-1` ([docs](https://docs.recall.ai/docs/regions)) |
| `RECALL_BOT_NAME` | no | Display name in the meeting. Default `Agent Notetaker`. |
| `RECALL_OUTPUT_DIR` | no | Where transcripts are written. Default `~/.agent/workspace/recall`. |

> **Region matters.** Keys, bots, recordings and transcripts all live in exactly
> one Recall region. A key used against the wrong region returns `401` with no
> explanation, which is why `RECALL_REGION` is required rather than guessed.
> Get your key from the dashboard for *your* region, e.g.
> `https://eu-central-1.recall.ai/dashboard/developers/api-keys`.

See `.env.example`. `is_configured()` tells you whether the environment is
usable, if you would rather hide the tools than offer ones that cannot work.

## Use it from an agent

Two exports are the whole integration surface:

- `TOOLS` — function-calling schemas, plain `{name, description, parameters}`
- `dispatch(name, args)` — runs one and returns a **JSON string**, ready to hand
  straight back as the tool result

```python
import json, os
from openai import OpenAI            # xAI's API is OpenAI-compatible
from agent_recall import TOOLS, dispatch

client = OpenAI(base_url="https://api.x.ai/v1", api_key=os.environ["XAI_API_KEY"])
tools = [{"type": "function", "function": t} for t in TOOLS]
messages = [{"role": "user", "content": "Record my standup: https://zoom.us/j/123"}]

while True:
    message = client.chat.completions.create(
        model="grok-4",               # or whichever Grok model you use
        messages=messages,
        tools=tools,
    ).choices[0].message
    messages.append(message)

    if not message.tool_calls:
        print(message.content)
        break

    for call in message.tool_calls:
        messages.append({
            "role": "tool",
            "tool_call_id": call.id,
            "content": dispatch(call.function.name, json.loads(call.function.arguments)),
        })
```

Other shapes work the same way — Anthropic wants `input_schema` instead of
`parameters`, so remap the key; a framework with its own registry can loop over
`HANDLERS` (name → callable). Handlers never raise: failures come back as
`{"success": false, "error": ...}`, so a bad tool call cannot break the loop.

`skills/notetaker/SKILL.md` covers the workflow in more depth — scheduling lead
time, waiting-room admission, how speaker labels are produced. Installing the
plugin registers it as the `notetaker` skill (`/notetaker` in Grok); otherwise
paste it into your system prompt.

## Tools

| Tool | What it does |
|---|---|
| `recall_schedule_bot` | Send a bot to a meeting, now or at a given time |
| `recall_bot_status` | Where the bot is in its lifecycle |
| `recall_list_bots` | Scheduled and recently finished bots |
| `recall_get_transcript` | Start transcription and fetch the result |
| `recall_cancel_bot` | Unschedule, or pull a bot out of a call |

Transcripts are written to `$RECALL_OUTPUT_DIR/<bot_id>/` — by default
`~/.agent/workspace/recall/<bot_id>/` — as `transcript.txt` (readable) and
`transcript.json` (raw). Point `RECALL_OUTPUT_DIR` at your agent's workspace if
it keeps one — e.g. `$GROK_HOME/workspace/recall` under Grok.

## CLI

The same operations from a terminal, for testing and one-off scheduling:

```bash
agent-recall schedule 'https://zoom.us/j/123?pwd=abc' +45m
agent-recall list
agent-recall status <bot_id>
agent-recall transcript <bot_id>
agent-recall cancel <bot_id>
```

`python -m agent_recall ...` works identically, without installing the script.

## Tests

```bash
python3 -m unittest discover -s tests
```

Stdlib `unittest`, no test dependency. Each test pins a specific defect — the
retry budget, transcript encoding, the MCP wire format, bot-id validation — and
the network is never touched.

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

**Stdlib only.** No third-party HTTP dependency — and no MCP SDK either. The
stdio transport is newline-delimited JSON-RPC, so `agent_recall/mcp_server.py`
implements the four methods a host actually calls (`initialize`, `ping`,
`tools/list`, `tools/call`) in under 200 lines, and installing this adds nothing
to your agent's dependency tree.

**Tool failures are results, not protocol errors.** A failed call comes back as
content with `isError: true`, so the model reads the reason and corrects itself.
JSON-RPC errors are reserved for protocol misuse — including a `tools/call` for
a name that was never advertised, which returns `-32602`.

**One call cannot outlast an agent turn.** Recall answers a rate limit with
`Retry-After`, sometimes an hour of it. Honouring that literally would freeze
the caller, so retries are capped at 30s each and abandoned once a 45s budget is
spent — the tool returns an error the model can act on instead of going quiet.

**Polling, not webhooks.** Recall recommends webhooks for production, but that
needs an HTTP endpoint the agent owns, which is exactly the host coupling this
package avoids. Note that Recall sends the Standard Webhooks `webhook-*` header
spelling; a receiver that validates only the older `svix-*` spelling will fail
verification.

## License

MIT
