"""Thin Recall.ai API client.

Stdlib only — no third-party HTTP dependency, so the plugin adds nothing to the
Hermes dependency tree.

Configuration comes entirely from the environment:

    RECALL_API_KEY   required, region-scoped
    RECALL_REGION    required, one of the four Recall regions
    RECALL_BOT_NAME  optional, defaults to "Hermes Notetaker"
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

# Recall runs four independent regions. Keys, bots, recordings and transcripts
# all live in exactly one of them; a key used against the wrong region returns
# 401 with no hint as to why, so we validate up front.
REGIONS = ("us-east-1", "us-west-2", "eu-central-1", "ap-northeast-1")

DEFAULT_BOT_NAME = "Hermes Notetaker"

# A bot booked more than 10 minutes ahead is a "scheduled" bot: Recall reserves
# a machine for it, so it is guaranteed to join on time and is exempt from the
# concurrency cap. Anything closer is an "ad-hoc" bot drawn from a small pool of
# warm machines that can run dry (HTTP 507).
SCHEDULED_THRESHOLD_SECONDS = 10 * 60

# Bots take a few seconds to boot and navigate to the meeting, so aim slightly
# early rather than exactly on the hour.
JOIN_LEAD_SECONDS = 15

# Bot statuses that end the lifecycle.
TERMINAL_BOT_STATUSES = frozenset({"done", "fatal"})

# 507 means the ad-hoc bot pool is momentarily empty. Recall replenishes it
# continuously, so the documented remedy is to retry after ~30s.
RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504, 507})


class RecallError(Exception):
    """Any failure that should be reported to the caller as an error result."""


class RecallConfigError(RecallError):
    """Configuration is missing or invalid — distinct from an API failure."""


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def region() -> str:
    value = (os.environ.get("RECALL_REGION") or "").strip()
    if not value:
        raise RecallConfigError(
            "RECALL_REGION is not set. Choose the region your Recall workspace "
            f"lives in: {', '.join(REGIONS)}."
        )
    if value not in REGIONS:
        raise RecallConfigError(
            f"RECALL_REGION={value!r} is not a Recall region. "
            f"Expected one of: {', '.join(REGIONS)}."
        )
    return value


def api_key() -> str:
    key = (os.environ.get("RECALL_API_KEY") or "").strip()
    if not key:
        raise RecallConfigError(
            "RECALL_API_KEY is not set. Create one in the Recall dashboard for "
            "the same region as RECALL_REGION."
        )
    return key


def bot_name() -> str:
    return (os.environ.get("RECALL_BOT_NAME") or "").strip() or DEFAULT_BOT_NAME


def api_base() -> str:
    return f"https://{region()}.recall.ai/api/v1"


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def request(method: str, path: str, body=None, max_attempts: int = 4):
    """
    Call the Recall API and return parsed JSON (or None for an empty body).

    Retries the status codes Recall documents as retryable, honouring
    Retry-After when present. Raises RecallError on anything unrecoverable —
    callers turn that into an error result rather than letting it propagate.
    """
    url = path if path.startswith("http") else api_base() + path
    payload = json.dumps(body).encode() if body is not None else None
    key = api_key()

    for attempt in range(1, max_attempts + 1):
        req = urllib.request.Request(url, data=payload, method=method)
        req.add_header("Authorization", f"Token {key}")
        req.add_header("Accept", "application/json")
        if payload is not None:
            req.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None

        except urllib.error.HTTPError as err:
            detail = err.read().decode(errors="replace")[:500]

            if err.code in RETRYABLE_STATUSES and attempt < max_attempts:
                retry_after = err.headers.get("Retry-After")
                if retry_after:
                    delay = float(retry_after)
                elif err.code == 507:
                    delay = 30.0
                else:
                    delay = min(2 ** attempt, 15)
                time.sleep(delay)
                continue

            if err.code == 401:
                raise RecallError(
                    f"401 Unauthorized. RECALL_API_KEY is not valid for region "
                    f"{region()} — Recall keys are region-scoped, so check that "
                    f"RECALL_REGION matches the dashboard the key came from."
                ) from err
            if err.code == 404:
                raise RecallError(f"404 Not Found: {url}") from err
            if err.code == 507:
                raise RecallError(
                    "507 — no ad-hoc bot available right now. Recall keeps a "
                    "small pool of warm machines for bots joining within 10 "
                    "minutes. Schedule further ahead, or retry in ~30s."
                ) from err
            raise RecallError(f"HTTP {err.code} from {method} {path}: {detail}") from err

        except urllib.error.URLError as err:
            if attempt < max_attempts:
                time.sleep(min(2 ** attempt, 15))
                continue
            raise RecallError(f"Network error calling {url}: {err.reason}") from err

    raise RecallError(f"Gave up calling {method} {path} after {max_attempts} attempts")


def download(url: str):
    """Fetch a transcript download URL. These are presigned — send no auth."""
    try:
        with urllib.request.urlopen(url, timeout=120) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, ValueError) as err:
        raise RecallError(f"Failed to download transcript: {err}") from err


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
        return datetime.now(timezone.utc) + timedelta(
            seconds=int(match.group(1)) * units[match.group(2)])

    try:
        parsed = parse_iso(str(value).strip())
    except ValueError as err:
        raise RecallConfigError(
            f"Could not read the time {value!r}. Use 'now', an offset like "
            f"'+45m', or an ISO 8601 timestamp like 2026-08-29T14:00."
        ) from err

    if parsed.tzinfo is None:
        parsed = parsed.astimezone()  # naive input means local time
    return parsed.astimezone(timezone.utc)


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
