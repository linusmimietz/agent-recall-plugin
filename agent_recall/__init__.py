"""Recall.ai meeting notetaker — a host-agnostic tool bundle.

Sends a Recall.ai bot into a Zoom / Google Meet / Microsoft Teams call to record
it, then fetches a speaker-labelled transcript once the meeting has ended.

Explicit by design: the bot only joins meeting URLs passed to it. There is no
calendar scanning and no auto-dial.

There is no host plugin API here. ``TOOLS`` holds plain function-calling schemas
and ``dispatch`` runs one by name, which is all any agent needs::

    import json
    from agent_recall import TOOLS, dispatch

    tools = [{"type": "function", "function": t} for t in TOOLS]   # xAI/OpenAI
    ...
    result = dispatch(call.function.name, json.loads(call.function.arguments))
    # -> a JSON string, ready to hand back as the tool message

All configuration is environment-driven: ``RECALL_API_KEY`` and
``RECALL_REGION`` are required, ``RECALL_BOT_NAME`` and ``RECALL_OUTPUT_DIR``
are optional. Call ``is_configured()`` if you would rather hide the tools than
offer ones that cannot work.

Every tool is one-shot and returns immediately. Recall work that takes time —
the meeting running, the transcript being generated — is reported as
``status: "processing"`` with a ``retry_after_seconds`` hint, so the agent
decides when to come back rather than a handler blocking the turn.
"""

from __future__ import annotations

import json
import logging

from . import recall_api as api
from . import schemas, tools, transcript

__version__ = "2.1.0"

logger = logging.getLogger(__name__)

# Schema and handler are declared together so the two exported views below
# cannot drift apart.
_TOOLS = (
    (schemas.SCHEDULE_BOT,   tools.schedule_bot),
    (schemas.BOT_STATUS,     tools.bot_status),
    (schemas.LIST_BOTS,      tools.list_bots),
    (schemas.GET_TRANSCRIPT, tools.get_transcript),
    (schemas.CANCEL_BOT,     tools.cancel_bot),
)

#: Function-calling schemas, in the ``{name, description, parameters}`` shape
#: that xAI, OpenAI and Anthropic-style APIs all accept (wrap as needed).
TOOLS = tuple(schema for schema, _ in _TOOLS)

#: Tool name -> handler. Each handler takes an args dict and returns a JSON
#: string; none of them raise.
HANDLERS = {schema["name"]: handler for schema, handler in _TOOLS}

__all__ = [
    "TOOLS", "HANDLERS", "dispatch", "is_configured", "__version__",
    "api", "schemas", "tools", "transcript",
]


def dispatch(name: str, args=None) -> str:
    """Run the named tool and return its JSON result string.

    An unknown name is answered with the same error envelope the handlers use,
    so a model that hallucinates a tool name gets a correctable answer rather
    than an exception through the agent loop.
    """
    handler = HANDLERS.get(name)
    if handler is None:
        return json.dumps({
            "success": False,
            "error": f"Unknown tool {name!r}. Available: {', '.join(HANDLERS)}.",
        })
    return handler(args or {})


def is_configured() -> bool:
    """Whether the required environment is present and valid.

    A value can be set but wrong — a region typo, most likely. Checking up
    front lets a caller keep a broken configuration out of the model's tool
    list rather than surfacing it as an error on first use.
    """
    try:
        api.region()
        api.api_key()
        return True
    except api.RecallConfigError as err:
        logger.info("agent_recall: not configured — %s", err)
        return False
