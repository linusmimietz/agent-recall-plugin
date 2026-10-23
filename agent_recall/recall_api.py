"""Thin Recall.ai API client.

Stdlib only — no third-party HTTP dependency, so the plugin adds nothing to the
agent's dependency tree.

Configuration comes from the environment, or per request from ``credentials``:

    RECALL_API_KEY   required, region-scoped
    RECALL_REGION    required, one of the four Recall regions
    RECALL_BOT_NAME  optional, defaults to "Agent Notetaker"
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

# Recall runs four independent regions. Keys, bots, recordings and transcripts
# all live in exactly one of them; a key used against the wrong region returns
# 401 with no hint as to why, so we validate up front.
REGIONS = ("us-east-1", "us-west-2", "eu-central-1", "ap-northeast-1")

DEFAULT_BOT_NAME = "Agent Notetaker"

# A bot booked more than 10 minutes ahead is a "scheduled" bot: Recall reserves
# a machine for it, so it is guaranteed to join on time and is exempt from the
# concurrency cap. Anything closer is an "ad-hoc" bot drawn from a small pool of
# warm machines that can run dry (HTTP 507).
SCHEDULED_THRESHOLD_SECONDS = 10 * 60

# Bots take a few seconds to boot and navigate to the meeting, so aim slightly
# early rather than exactly on the hour.
JOIN_LEAD_SECONDS = 15

# Bot statuses that end the lifecycle. media_expired and analysis_failed are
# final too: without them a bot in either state looks like work still in
# progress, and the retry hint tells the model to come back forever.
TERMINAL_BOT_STATUSES = frozenset({
    "done", "fatal", "media_expired", "analysis_failed",
})

# Statuses that legitimately mean "still working". Recall's docs warn that
# status codes are not an enum and new ones appear without notice, so progress
# is decided by this allowlist rather than by absence from the terminal set —
# an unrecognised code must not be mistaken for a bot still on its way.
IN_PROGRESS_BOT_STATUSES = frozenset({
    "ready", "joining_call", "in_waiting_room", "in_call_not_recording",
    "in_call_recording", "recording_permission_allowed", "call_ended",
    "recording_done", "analysis_done",
})

# 507 means the ad-hoc bot pool is momentarily empty. Recall replenishes it
# continuously, so the documented remedy is to retry after ~30s. 409 is
# documented as "please retry with exponential backoff".
RETRYABLE_STATUSES = frozenset({408, 409, 429, 500, 502, 503, 504, 507})

# A tool call runs inside an agent turn, so the whole retry sequence has to fit
# a budget the host will wait out. Recall can answer 429 with
# "Retry-After: 3600"; honouring that literally would freeze the caller for an
# hour, so the delay is capped and the loop stops once the budget is spent.
REQUEST_TIMEOUT_SECONDS = 15
DOWNLOAD_TIMEOUT_SECONDS = 60
MAX_RETRY_DELAY_SECONDS = 30
REQUEST_BUDGET_SECONDS = 45

# Transcripts are JSON of a meeting's words; tens of MB is already implausible,
# and an unbounded read would let a bad response exhaust memory.
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024

# Listings are paginated. Following links forever would blow the budget, so we
# follow a bounded number and tell the caller when the view is partial.
MAX_LIST_PAGES = 5

# Offsets are model-supplied; "+999999999d" overflows timedelta.
MAX_OFFSET_SECONDS = 365 * 86400

# Recall bot ids are UUIDs. Validating them keeps a hallucinated or hostile id
# out of both the request path and the transcript directory name.
BOT_ID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class RecallError(Exception):
    """Any failure that should be reported to the caller as an error result."""


class RecallConfigError(RecallError):
    """Configuration is missing or invalid — distinct from an API failure."""


class RecallArgumentError(RecallError):
    """A tool argument is malformed. The model can fix this and call again."""


class RecallHTTPError(RecallError):
    """An HTTP failure the caller may want to branch on, e.g. 405 on DELETE."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


class RecallExpiredUrlError(RecallError):
    """A presigned download URL has expired; ask Recall for a fresh one."""


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

# Per-request configuration set by the HTTP transport, where each request
# carries its caller's key. A ContextVar rather than a global because requests
# are served on concurrent threads, and each must see only its own key.
_overrides: contextvars.ContextVar = contextvars.ContextVar(
    "agent_recall_overrides", default=None)


@contextlib.contextmanager
def credentials(**values):
    """Use ``values`` (e.g. ``RECALL_API_KEY="..."``) ahead of the environment.

    Names left out, or set empty, still fall back to the environment, so a
    server can supply the region while each caller supplies the key.
    """
    token = _overrides.set({k: str(v).strip() for k, v in values.items() if v})
    try:
        yield
    finally:
        _overrides.reset(token)


def _env(name: str) -> str:
    """Read configuration: per-request overrides first, then the environment.

    A host that prompts for plugin configuration (Claude Code's ``userConfig``)
    exports the answers as ``CLAUDE_PLUGIN_OPTION_<NAME>`` to the MCP
    subprocess, so that is honoured as a fallback. Substituting those values
    into the MCP manifest instead would be worse: a host that does not expand
    the placeholder would set the variable to the literal text and mask a
    correctly exported one.
    """
    override = (_overrides.get() or {}).get(name)
    if override:
        return override
    for key in (name, f"CLAUDE_PLUGIN_OPTION_{name}"):
        value = (os.environ.get(key) or "").strip()
        if value:
            return value
    return ""


def region() -> str:
    value = _env("RECALL_REGION")
    if not value:
        raise RecallConfigError(
            "RECALL_REGION (or the X-Recall-Region header) is not set. Choose "
            f"the region your Recall workspace lives in: {', '.join(REGIONS)}."
        )
    if value not in REGIONS:
        raise RecallConfigError(
            f"Region {value!r} is not a Recall region. "
            f"Expected one of: {', '.join(REGIONS)}."
        )
    return value


def api_key() -> str:
    key = _env("RECALL_API_KEY")
    if not key:
        raise RecallConfigError(
            "RECALL_API_KEY is not set. Create one in the Recall dashboard for "
            "the same region as RECALL_REGION."
        )
    return key


def bot_name() -> str:
    return _env("RECALL_BOT_NAME") or DEFAULT_BOT_NAME


def validate_bot_id(value) -> str:
    """Return ``value`` as a known-safe bot id, or explain why it is not one."""
    bot_id = str(value or "").strip()
    if not BOT_ID_PATTERN.match(bot_id):
        raise RecallArgumentError(
            f"{str(value or '')!r} is not a Recall bot id. Ids are UUIDs like "
            f"'3f2a1b4c-5d6e-4f80-9a1b-2c3d4e5f6071' — call recall_list_bots to "
            f"find the one you want."
        )
    return bot_id


def api_base() -> str:
    return f"https://{region()}.recall.ai/api/v1"


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def _retry_after_seconds(header: str):
    """Read a Retry-After header. Returns None when it cannot be understood.

    RFC 7231 allows either a number of seconds or an HTTP-date, and proxies and
    WAFs in front of an API do emit the date form even when the API itself
    documents seconds.
    """
    try:
        return float(header)
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (when - datetime.now(timezone.utc)).total_seconds()


def _retry_delay(err, attempt: int) -> float:
    """How long to wait before retrying, capped to keep the call answerable."""
    header = err.headers.get("Retry-After") if err.headers else None
    delay = _retry_after_seconds(header) if header else None
    if delay is None:
        delay = 30.0 if err.code == 507 else min(2 ** attempt, 15)
    return max(0.0, min(delay, MAX_RETRY_DELAY_SECONDS))


def _may_retry(attempt: int, max_attempts: int, delay: float, deadline: float) -> bool:
    """Whether another attempt fits both the attempt count and the time budget."""
    return attempt < max_attempts and time.monotonic() + delay <= deadline


def request(method: str, path: str, body=None, max_attempts: int = 4):
    """
    Call the Recall API and return parsed JSON (or None for an empty body).

    Retries the status codes Recall documents as retryable, honouring
    Retry-After when present but never sleeping past REQUEST_BUDGET_SECONDS in
    total. Raises RecallError on anything unrecoverable — callers turn that into
    an error result rather than letting it propagate.
    """
    url = path if path.startswith("http") else api_base() + path
    payload = json.dumps(body).encode() if body is not None else None
    key = api_key()
    deadline = time.monotonic() + REQUEST_BUDGET_SECONDS

    for attempt in range(1, max_attempts + 1):
        req = urllib.request.Request(url, data=payload, method=method)
        req.add_header("Authorization", f"Token {key}")
        req.add_header("Accept", "application/json")
        if payload is not None:
            req.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None

        except urllib.error.HTTPError as err:
            detail = err.read().decode(errors="replace")[:500]

            if err.code in RETRYABLE_STATUSES:
                delay = _retry_delay(err, attempt)
                if _may_retry(attempt, max_attempts, delay, deadline):
                    time.sleep(delay)
                    continue

            if err.code == 401:
                raise RecallError(
                    f"401 Unauthorized. The Recall API key is not valid for "
                    f"region {region()} — Recall keys are region-scoped, so check "
                    f"that the region matches the dashboard the key came from."
                ) from err
            if err.code == 404:
                raise RecallError(f"404 Not Found: {url}") from err
            if err.code == 402:
                raise RecallError(
                    "402 — the Recall workspace is out of credit. Top it up in "
                    "the dashboard for this region before scheduling more bots."
                ) from err
            if err.code == 403:
                raise RecallError(
                    f"403 Forbidden from {method} {path}. If the body mentions "
                    f"request_blocked this is Recall's WAF rather than your key: "
                    f"{detail}"
                ) from err
            if err.code == 507:
                raise RecallError(
                    "507 — no ad-hoc bot available right now. Recall keeps a "
                    "small pool of warm machines for bots joining within 10 "
                    "minutes. Schedule further ahead, or retry in ~30s."
                ) from err
            raise RecallHTTPError(
                f"HTTP {err.code} from {method} {path}: {detail}", err.code) from err

        # URLError and socket timeouts are both OSError; a read that times out
        # mid-response raises TimeoutError, which is not a URLError at all.
        except OSError as err:
            delay = min(2 ** attempt, MAX_RETRY_DELAY_SECONDS)
            if _may_retry(attempt, max_attempts, delay, deadline):
                time.sleep(delay)
                continue
            raise RecallError(
                f"Network error calling {url}: {getattr(err, 'reason', err)}") from err

        except ValueError as err:
            raise RecallError(
                f"{method} {path} did not return valid JSON: {err}") from err

    raise RecallError(f"Gave up calling {method} {path} after {max_attempts} attempts")


def paginate(path: str, max_pages: int = MAX_LIST_PAGES):
    """Follow Recall's ``next`` links. Returns ``(rows, truncated)``.

    Reading only the first page silently undercounts, which matters most for
    recall_list_bots: the skill tells the model to use it to decide whether a
    meeting is already covered, and an undercount causes the double-booking
    that advice exists to prevent.
    """
    rows = []
    url = path
    for _ in range(max_pages):
        page = request("GET", url) or {}
        rows.extend(page.get("results") or [])
        following = page.get("next")
        if not following:
            return rows, False
        url = following
    return rows, True


def download(url: str):
    """Fetch a transcript download URL. These are presigned — send no auth."""
    if not str(url).startswith("https://"):
        raise RecallError("Refusing to download a transcript over a non-HTTPS URL.")
    try:
        with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as resp:
            raw = resp.read(MAX_DOWNLOAD_BYTES + 1)
        if len(raw) > MAX_DOWNLOAD_BYTES:
            raise RecallError(
                f"Transcript is larger than the "
                f"{MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB ceiling; refusing to load it.")
        return json.loads(raw)
    except urllib.error.HTTPError as err:
        if err.code in (403, 404):
            # Recall presigns these for six hours and will mint a fresh one on
            # request, so this is recoverable rather than a permissions problem.
            raise RecallExpiredUrlError(
                f"The transcript download URL has expired (HTTP {err.code})."
            ) from err
        raise RecallError(
            f"Failed to download transcript: HTTP {err.code}") from err
    except OSError as err:
        raise RecallError(
            f"Failed to download transcript: {getattr(err, 'reason', err)}") from err
    except ValueError as err:
        raise RecallError(f"Transcript was not valid JSON: {err}") from err


# --------------------------------------------------------------------------
# Time helpers
# --------------------------------------------------------------------------

def iso(dt: datetime) -> str:
    """Recall wants ISO 8601 ending in Z, which isoformat() won't give you."""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(text: str) -> datetime:
    """Read one of Recall's timestamps back into an aware datetime."""
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def seconds_until(dt: datetime) -> float:
    return (dt - datetime.now(timezone.utc)).total_seconds()


def parse_when(value):
    """
    Turn a human-written time into an aware UTC datetime.

    Accepts None/"now", a relative offset ("+45m", "+2h", "+3d"), or an ISO 8601
    timestamp. A timestamp without a timezone is read as local time.
    """
    if not value or value == "now":
        return datetime.now(timezone.utc)

    match = re.fullmatch(r"\+(\d+)\s*([smhd])", str(value).strip())
    if match:
        units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
        seconds = int(match.group(1)) * units[match.group(2)]
        if seconds > MAX_OFFSET_SECONDS:
            raise RecallArgumentError(
                f"{value!r} is further ahead than the "
                f"{MAX_OFFSET_SECONDS // 86400}-day limit for a scheduled bot."
            )
        return datetime.now(timezone.utc) + timedelta(seconds=seconds)

    try:
        parsed = parse_iso(str(value).strip())
    except ValueError as err:
        raise RecallArgumentError(
            f"Could not read the time {value!r}. Use 'now', an offset like "
            f"'+45m', or an ISO 8601 timestamp like 2026-08-29T14:00."
        ) from err

    if parsed.tzinfo is None:
        parsed = parsed.astimezone()  # naive input means local time
    return parsed.astimezone(timezone.utc)


def format_offset(seconds) -> str:
    """Like format_duration, but says which side of now it falls on.

    format_duration takes an absolute value, so a join time that has already
    passed would otherwise read as one comfortably in the future.
    """
    rendered = format_duration(seconds)
    return f"{rendered} ago" if seconds < 0 else rendered


def format_duration(seconds) -> str:
    seconds = int(abs(seconds))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{round(seconds / 60)}m"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h"
    return f"{round(seconds / 86400)}d"


# --------------------------------------------------------------------------
# Bot helpers
# --------------------------------------------------------------------------

def latest_status(bot: dict) -> dict:
    changes = bot.get("status_changes") or []
    return changes[-1] if changes else {}


def is_dispatched(bot: dict) -> bool:
    """True once the bot has started joining; scheduled bots have no status yet."""
    return bool(bot.get("status_changes"))


def describe_meeting(bot: dict) -> str:
    """
    Render a bot's meeting in one line.

    Recall returns meeting_url as a structured object (platform, meeting_id,
    password), and clears it entirely a few days after the call — at which point
    we fall back to the URL stashed in metadata at creation time.
    """
    url = bot.get("meeting_url")
    if isinstance(url, dict) and url.get("meeting_id"):
        return f"{url.get('platform', '?')} {url['meeting_id']}"
    if isinstance(url, str) and url:
        return url
    stashed = (bot.get("metadata") or {}).get("meeting_url")
    return stashed or "(expired)"


def first_recording(bot: dict):
    recordings = bot.get("recordings") or []
    return recordings[0] if recordings else None
