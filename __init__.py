"""Recall.ai notetaker plugin for Hermes.

Sends a Recall.ai bot into a Zoom / Google Meet / Microsoft Teams call to record
it, then fetches a speaker-labelled transcript once the meeting has ended.

Explicit by design: the bot only joins meeting URLs passed to it. There is no
calendar scanning and no auto-dial.

All configuration is environment-driven — ``RECALL_API_KEY`` and
``RECALL_REGION`` are required (both declared in ``plugin.yaml`` so
``hermes plugins install`` prompts for them), ``RECALL_BOT_NAME`` is optional.

Every tool is one-shot and returns immediately. Recall work that takes time —
the meeting running, the transcript being generated — is reported as
``status: "processing"`` with a ``retry_after_seconds`` hint, so the agent
decides when to come back rather than a handler blocking the turn.
"""

from __future__ import annotations

import logging
from pathlib import Path

from . import recall_api as api
from . import schemas, tools
from .cli import recall_command as _recall_command
from .cli import register_cli as _register_recall_cli

logger = logging.getLogger(__name__)

_TOOLS = (
    ("recall_schedule_bot",  schemas.SCHEDULE_BOT,   tools.schedule_bot,   "📅"),
    ("recall_bot_status",    schemas.BOT_STATUS,     tools.bot_status,     "🟢"),
    ("recall_list_bots",     schemas.LIST_BOTS,      tools.list_bots,      "📋"),
    ("recall_get_transcript", schemas.GET_TRANSCRIPT, tools.get_transcript, "📝"),
    ("recall_cancel_bot",    schemas.CANCEL_BOT,     tools.cancel_bot,     "🚫"),
)


def _is_configured() -> bool:
    """Hide the tools when the plugin cannot possibly work.

    ``requires_env`` in plugin.yaml already gates loading on these being set,
    but a value can still be present and invalid (a region typo, most likely).
    Checking here keeps a broken configuration out of the model's tool list
    rather than surfacing it as a run-time error on first use.
    """
    try:
        api.region()
        api.api_key()
        return True
    except api.RecallConfigError as err:
        logger.info("recall plugin: tools hidden — %s", err)
        return False


def register(ctx) -> None:
    """Register tools, the bundled skill, and the ``hermes recall`` CLI."""
    for name, schema, handler, emoji in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="recall",
            schema=schema,
            handler=handler,
            check_fn=_is_configured,
            emoji=emoji,
        )

    # Plugin skills are resolvable as "<plugin>:<name>" but are deliberately not
    # listed in the system prompt's skill index, so the tool descriptions point
    # at this one by name.
    skill_path = Path(__file__).parent / "SKILL.md"
    if skill_path.exists():
        try:
            ctx.register_skill(
                name="notetaker",
                path=skill_path,
                description=(
                    "How to record and transcribe a meeting with Recall.ai: "
                    "scheduling lead time, waiting-room admission, and how "
                    "speaker labels are produced."
                ),
            )
        except Exception as err:  # pragma: no cover — skill API is optional
            logger.debug("recall plugin: skill registration failed: %s", err)

    ctx.register_cli_command(
        name="recall",
        help="Recall.ai notetaker (schedule, list, status, transcript, cancel)",
        setup_fn=_register_recall_cli,
        handler_fn=_recall_command,
        description=(
            "Send a Recall.ai bot to a meeting and fetch its transcript. "
            "See: hermes recall --help"
        ),
    )
