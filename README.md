# agent-recall-plugin

Meeting-notetaker tools for any agent. Sends a [Recall.ai](https://recall.ai)
bot into a Zoom, Google Meet or Microsoft Teams call to record it, then fetches
a **speaker-labelled transcript** afterwards.

Stdlib-only Python. Plugs into [Grok](https://docs.x.ai/build/features/skills-plugins-marketplaces),
Claude Code and Cursor as an **MCP server** — spawned over stdio, or reached by
URL over Streamable HTTP — or drops into your own agent loop as a tool table and
a dispatcher.

Explicit by design: the bot only joins meeting URLs you pass it. No calendar
scanning, no auto-dial.

## Install

### Grok (and Claude Code, Cursor)

These hosts don't load Python plugins — they spawn an MCP server and talk
JSON-RPC to it. This repo ships one, plus the manifests that wire it up.

**As a plugin** (also installs the `recall` skill):

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
| `skills/recall/SKILL.md` | The bundled skill |

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

### Grok Team Bot (and other URL-based hosts)

A Team Bot cannot pass secrets to a command it spawns, so the stdio server above
never sees a key there. Instead run the HTTP server and connect to it by URL,
with the key in a header:

```bash
pip install git+https://github.com/linusmimietz/agent-recall-plugin
agent-recall-http        # serves http://127.0.0.1:8000/mcp
```

Each request carries its caller's credentials; the server stores none.

| Header | Required | |
|---|---|---|
| `Authorization: Bearer <key>` | yes | Recall API key. Requests without one get `401`. |
| `X-Recall-Region` | unless the server has `RECALL_REGION` | e.g. `eu-central-1` |
| `X-Recall-Bot-Name` | no | Falls back to `RECALL_BOT_NAME`, then `Agent Notetaker` |

The server's own `RECALL_API_KEY` is never used for an HTTP request, so nobody
can spend the host's key by leaving theirs out.

From the Grok CLI that is:

```bash
grok mcp add --transport http recall http://127.0.0.1:8000/mcp \
  --header "Authorization: Bearer ${RECALL_API_KEY}" \
  --header "X-Recall-Region: eu-central-1"
```

If you already added the stdio `recall` server, remove it first
(`grok mcp remove recall`) or give this one another name. In a Team Bot, give
it the same URL and headers.

**Local or hosted.** By default the server binds `127.0.0.1:8000` (`--host`,
`--port`, or `HOST`/`PORT`), which works when the agent's MCP client runs on the
same machine and the process stays up. If the Team Bot cannot reach a loopback
URL, deploy the bundled `Dockerfile` to any container host that terminates TLS —
Fly.io, Render, Cloud Run, or a Cloudflare Tunnel — and use its
`https://…/mcp` URL. The image binds `0.0.0.0:$PORT` and needs no secrets:

```bash
docker build -t agent-recall .
docker run -p 8080:8080 agent-recall      # then POST to http://localhost:8080/mcp
```

Never expose it over plain HTTP beyond your machine: the key is in a header.

The endpoint is stateless Streamable HTTP: `POST /mcp` takes one JSON-RPC
message and returns its JSON response (`202` with no body for a notification).
There are no sessions and no SSE stream, so `GET`/`DELETE` return `405`.

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

All configuration is environment-driven — nothing is stored in this repo. The
HTTP server takes the key (and optionally region and bot name) from request
headers instead; see above.

| Variable | Required | Description |
|---|---|---|
| `RECALL_API_KEY` | yes | Recall.ai API key. **Region-scoped.** |
| `RECALL_REGION` | yes | `us-east-1`, `us-west-2`, `eu-central-1` or `ap-northeast-1` ([docs](https://docs.recall.ai/docs/regions)) |
| `RECALL_BOT_NAME` | no | Display name in the meeting. Default `Agent Notetaker`. |
| `RECALL_OUTPUT_DIR` | no | Also write each transcript to disk here. Unset by default: nothing is written. |

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

`skills/recall/SKILL.md` covers the workflow in more depth — scheduling lead
time, waiting-room admission, how speaker labels are produced. Installing the
plugin registers it as the `recall` skill (`/recall` in Grok); otherwise paste it
into your system prompt.

## Tools

| Tool | What it does |
|---|---|
| `recall_schedule_bot` | Send a bot to a meeting, now or at a given time |
| `recall_bot_status` | Where the bot is in its lifecycle |
| `recall_list_bots` | Scheduled and recently finished bots |
| `recall_get_transcript` | Start transcription and fetch the result |
| `recall_cancel_bot` | Unschedule, or pull a bot out of a call |

`recall_get_transcript` returns the speaker-labelled text in the tool result,
one `[hh:mm:ss] Speaker: text` paragraph per turn. Long transcripts are paged:
while `has_more` is true, call again with `offset` set to `next_offset`
(`limit` defaults to 200 turns, at most 1000).

Nothing is written to disk unless you set `RECALL_OUTPUT_DIR`; then the first
page also writes `<dir>/<bot_id>/transcript.txt` (readable) and
`transcript.json` (raw) and returns their paths. Leave it unset on a shared
server, so it does not keep other people's meetings.

## CLI

The same operations from a terminal, for testing and one-off scheduling:

```bash
agent-recall schedule 'https://zoom.us/j/123?pwd=abc' +45m
agent-recall list
agent-recall status <bot_id>
agent-recall transcript <bot_id> [--offset N] [--limit N]
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
`tools/list`, `tools/call`) in about 200 lines, and installing this adds nothing
to your agent's dependency tree. `agent_recall/http_server.py` serves the same
handler over Streamable HTTP with `http.server`.

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
