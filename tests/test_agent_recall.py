"""Regression tests for the fixes from the 2026-09 review.

Stdlib unittest, so this runs with no test dependency::

    python3 -m unittest discover -s tests -v

Every test here pins a specific defect. The network is never touched: urlopen is
replaced, and time.sleep is recorded rather than slept.
"""

from __future__ import annotations

import email.message
import http.client
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent_recall
from agent_recall import http_server, mcp_server, recall_api as api, tools
from agent_recall import transcript as tx

REPO = Path(__file__).resolve().parent.parent
BOT_ID = "3f2a1b4c-5d6e-4f80-9a1b-2c3d4e5f6071"


# --------------------------------------------------------------------------
# Doubles
# --------------------------------------------------------------------------

class FakeResponse:
    """Stands in for the object urlopen yields as a context manager."""

    def __init__(self, payload: bytes = b"{}"):
        self._payload = payload

    def read(self, amt=None):
        return self._payload if amt is None else self._payload[:amt]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code: int, headers=None, body: bytes = b'{"detail":"nope"}'):
    message = email.message.Message()
    for key, value in (headers or {}).items():
        message[key] = value
    return urllib.error.HTTPError(
        "https://us-east-1.recall.ai/api/v1/bot/", code, "err", message,
        io.BytesIO(body))


class ApiTestCase(unittest.TestCase):
    """Configured environment, plus a fake clock that sleep advances.

    Recording sleeps without moving time would make the retry budget
    unobservable: the loop decides whether another wait fits by reading
    time.monotonic(), so a frozen clock lets it retry forever.
    """

    def setUp(self):
        self.env = mock.patch.dict(
            os.environ,
            {"RECALL_REGION": "us-east-1", "RECALL_API_KEY": "test-key"},
            clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

        self.slept = []
        self.now = 0.0

        def fake_sleep(seconds):
            self.slept.append(seconds)
            self.now += seconds

        for target, replacement in (("sleep", fake_sleep),
                                    ("monotonic", lambda: self.now)):
            patcher = mock.patch.object(api.time, target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)


# --------------------------------------------------------------------------
# H1 — transcripts must survive a non-UTF-8 locale, and never destroy a
#      previous good file on the way down
# --------------------------------------------------------------------------

class TranscriptWritingTests(unittest.TestCase):
    NON_ASCII = [{"speaker": "山田", "start": 0, "end": 1,
                  "text": "café — don’t"}]

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        env = mock.patch.dict(os.environ, {"RECALL_OUTPUT_DIR": self.dir})
        env.start()
        self.addCleanup(env.stop)

    def test_non_ascii_round_trips_regardless_of_locale(self):
        """H1: an explicit UTF-8 codec, not the locale's."""
        text_path, raw_path = tx.write_outputs(BOT_ID, self.NON_ASCII, self.NON_ASCII)
        self.assertIn("山田", text_path.read_text(encoding="utf-8"))
        self.assertEqual(json.loads(raw_path.read_text(encoding="utf-8")), self.NON_ASCII)

    def test_a_failed_write_leaves_the_previous_transcript_intact(self):
        """H1: write_text truncates before encoding, which destroyed good data."""
        tx.write_outputs(BOT_ID, self.NON_ASCII, self.NON_ASCII)
        good = tx.output_dir(BOT_ID) / "transcript.json"
        original = good.read_text(encoding="utf-8")

        with mock.patch.object(Path, "write_text",
                               side_effect=UnicodeEncodeError(
                                   "latin-1", "x", 0, 1, "boom")):
            with self.assertRaises(UnicodeEncodeError):
                tx.write_outputs(BOT_ID, self.NON_ASCII, self.NON_ASCII)

        self.assertEqual(good.read_text(encoding="utf-8"), original)
        self.assertGreater(good.stat().st_size, 0)

    def test_no_temp_files_survive(self):
        tx.write_outputs(BOT_ID, self.NON_ASCII, self.NON_ASCII)
        leftovers = list(tx.output_dir(BOT_ID).glob("*.tmp"))
        self.assertEqual(leftovers, [])

    # M10 — model-supplied ids must not escape the output directory
    def test_unsafe_bot_ids_cannot_escape(self):
        for bad in ("../../pwned", "a/b", "", ".", "..", ".hidden", "x\x00y"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                tx.output_dir(bad)


# --------------------------------------------------------------------------
# H2/H3/H4/M8 — the retry matrix, and the time budget
# --------------------------------------------------------------------------

class RetryTests(ApiTestCase):
    def test_http_date_retry_after_does_not_crash(self):
        """H2: RFC 7231 allows an HTTP-date; float() raised ValueError."""
        responses = [http_error(429, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
                     FakeResponse(b'{"ok":true}')]
        with mock.patch.object(api.urllib.request, "urlopen",
                               side_effect=responses):
            self.assertEqual(api.request("GET", "/bot/"), {"ok": True})
        self.assertEqual(len(self.slept), 1)

    def test_retry_after_is_capped(self):
        """H3: Retry-After: 3600 slept for an hour, three times over."""
        responses = [http_error(429, {"Retry-After": "3600"}),
                     FakeResponse(b'{"ok":true}')]
        with mock.patch.object(api.urllib.request, "urlopen",
                               side_effect=responses):
            api.request("GET", "/bot/")
        self.assertLessEqual(max(self.slept), api.MAX_RETRY_DELAY_SECONDS)

    def test_total_sleep_stays_inside_the_budget(self):
        """H3/H5: one tool call must stay answerable within an agent turn."""
        with mock.patch.object(api.urllib.request, "urlopen",
                               side_effect=lambda *a, **k: (_ for _ in ()).throw(
                                   http_error(503, {"Retry-After": "3600"}))):
            with self.assertRaises(api.RecallError):
                api.request("GET", "/bot/")
        self.assertLessEqual(sum(self.slept), api.REQUEST_BUDGET_SECONDS)
        self.assertGreater(len(self.slept), 0, "should have retried at least once")

    def test_read_timeout_is_retried_then_reported(self):
        """H4: TimeoutError is an OSError but not a URLError, so it escaped."""
        responses = [TimeoutError("timed out"), FakeResponse(b'{"ok":true}')]
        with mock.patch.object(api.urllib.request, "urlopen",
                               side_effect=responses):
            self.assertEqual(api.request("GET", "/bot/"), {"ok": True})

        with mock.patch.object(api.urllib.request, "urlopen",
                               side_effect=TimeoutError("timed out")):
            with self.assertRaises(api.RecallError):
                api.request("GET", "/bot/")

    def test_409_is_retryable(self):
        """M8: Recall documents 409 as retry-with-backoff."""
        self.assertIn(409, api.RETRYABLE_STATUSES)
        responses = [http_error(409), FakeResponse(b'{"ok":true}')]
        with mock.patch.object(api.urllib.request, "urlopen",
                               side_effect=responses):
            self.assertEqual(api.request("GET", "/bot/"), {"ok": True})

    def test_malformed_json_is_a_recall_error(self):
        with mock.patch.object(api.urllib.request, "urlopen",
                               return_value=FakeResponse(b"<html>nope")):
            with self.assertRaises(api.RecallError):
                api.request("GET", "/bot/")

    def test_generic_http_failure_carries_its_status(self):
        """M7 depends on being able to branch on 405."""
        with mock.patch.object(api.urllib.request, "urlopen",
                               side_effect=http_error(405)):
            with self.assertRaises(api.RecallHTTPError) as caught:
                api.request("DELETE", f"/bot/{BOT_ID}/")
        self.assertEqual(caught.exception.status, 405)

    def test_api_key_never_appears_in_an_error(self):
        with mock.patch.object(api.urllib.request, "urlopen",
                               side_effect=http_error(400, body=b'{"detail":"bad"}')):
            with self.assertRaises(api.RecallError) as caught:
                api.request("GET", "/bot/")
        self.assertNotIn("test-key", str(caught.exception))


class DownloadTests(ApiTestCase):
    def test_expired_url_is_distinguishable(self):
        """M6: a 403 on a presigned URL is recoverable, not a permissions wall."""
        with mock.patch.object(api.urllib.request, "urlopen",
                               side_effect=http_error(403)):
            with self.assertRaises(api.RecallExpiredUrlError):
                api.download("https://example.com/t.json")

    def test_non_https_is_refused(self):
        """L10: the URL comes from Recall, but file:// should never be opened."""
        for url in ("file:///etc/passwd", "http://example.com/t.json"):
            with self.subTest(url=url), self.assertRaises(api.RecallError):
                api.download(url)

    def test_oversized_transcript_is_refused(self):
        """L10: an unbounded read could exhaust memory."""
        with mock.patch.object(api, "MAX_DOWNLOAD_BYTES", 16):
            with mock.patch.object(api.urllib.request, "urlopen",
                                   return_value=FakeResponse(b"x" * 64)):
                with self.assertRaises(api.RecallError):
                    api.download("https://example.com/t.json")


# --------------------------------------------------------------------------
# M4/M5 — parse_when over exactly what the tool description advertises
# --------------------------------------------------------------------------

class ParseWhenTests(ApiTestCase):
    def test_advertised_forms_all_parse(self):
        for value in (None, "now", "+45m", "+2h", "+3d", "+30s",
                      "2026-08-29T14:00", "2026-08-29T14:00:00Z"):
            with self.subTest(value=value):
                self.assertIsNotNone(api.parse_when(value))

    def test_absurd_offset_is_an_argument_error(self):
        """M4: '+999999999d' overflowed timedelta and leaked OverflowError."""
        with self.assertRaises(api.RecallArgumentError):
            api.parse_when("+999999999d")

    def test_unreadable_time_is_an_argument_error(self):
        with self.assertRaises(api.RecallArgumentError):
            api.parse_when("next tuesday-ish")

    def test_past_times_are_labelled_as_past(self):
        """M5: format_duration's abs() reported a past join as '2443d' away."""
        self.assertTrue(api.format_offset(-211114123).endswith("ago"))
        self.assertFalse(api.format_offset(3600).endswith("ago"))


# --------------------------------------------------------------------------
# H6 — status handling, including codes Recall adds without notice
# --------------------------------------------------------------------------

class StatusTests(ApiTestCase):
    def _status(self, code):
        bot = {"id": BOT_ID, "status_changes": [{"code": code}],
               "recordings": [], "meeting_url": {"platform": "zoom",
                                                 "meeting_id": "1"}}
        with mock.patch.object(api, "request", return_value=bot):
            return json.loads(tools.bot_status({"bot_id": BOT_ID}))

    def test_media_expired_is_terminal(self):
        """H6: it was reported as processing with a retry hint, forever."""
        result = self._status("media_expired")
        self.assertTrue(result["finished"])
        self.assertNotIn("retry_after_seconds", result)

    def test_analysis_failed_is_terminal(self):
        result = self._status("analysis_failed")
        self.assertTrue(result["finished"])
        self.assertNotIn("retry_after_seconds", result)

    def test_unknown_status_is_not_described_as_not_started(self):
        """H6: any future code read as 'has not started joining yet'."""
        result = self._status("some_future_code")
        self.assertIn("Unrecognised", result["meaning"])
        self.assertNotIn("retry_after_seconds", result)

    def test_permission_denied_gets_an_action_instead_of_a_retry(self):
        result = self._status("recording_permission_denied")
        self.assertIn("action_required", result)
        self.assertNotIn("retry_after_seconds", result)

    def test_in_progress_status_still_gets_a_retry_hint(self):
        result = self._status("in_call_recording")
        self.assertFalse(result["finished"])
        self.assertEqual(result["retry_after_seconds"], tools.RETRY_BOT_SECONDS)

    def test_every_documented_status_has_a_meaning(self):
        for code in api.IN_PROGRESS_BOT_STATUSES | api.TERMINAL_BOT_STATUSES:
            with self.subTest(code=code):
                self.assertIn(code, tools._STATUS_MEANING)


# --------------------------------------------------------------------------
# M1/M2/M3 — listing
# --------------------------------------------------------------------------

class ListBotsTests(ApiTestCase):
    def test_scheduled_list_follows_pagination(self):
        """M2: only the first page was read, so counts were simply wrong."""
        page_one = {"results": [{"id": BOT_ID, "join_at": "2030-01-01T00:00:00Z"}],
                    "next": "https://us-east-1.recall.ai/api/v1/bot/?page=2"}
        page_two = {"results": [{"id": BOT_ID, "join_at": "2030-01-02T00:00:00Z"}],
                    "next": None}
        with mock.patch.object(api, "request", side_effect=[page_one, page_two, {}]):
            result = json.loads(tools.list_bots({}))
        self.assertEqual(result["scheduled_count"], 2)

    def test_rows_without_join_at_are_skipped(self):
        """M3: bot['join_at'] raised KeyError and killed the whole listing."""
        page = {"results": [{"id": BOT_ID}, {"id": BOT_ID,
                                             "join_at": "2030-01-01T00:00:00Z"}],
                "next": None}
        with mock.patch.object(api, "request", side_effect=[page, {}]):
            result = json.loads(tools.list_bots({}))
        self.assertTrue(result["success"])
        self.assertEqual(result["scheduled_count"], 1)

    def test_recent_is_capped_locally(self):
        """M1: ?limit=10 is not a parameter GET /bot/ accepts."""
        rows = [{"id": BOT_ID, "status_changes": [{"code": "done"}], "recordings": []}
                for _ in range(50)]
        with mock.patch.object(api, "request",
                               side_effect=[{"results": [], "next": None},
                                            {"results": rows}]):
            result = json.loads(tools.list_bots({}))
        self.assertEqual(result["recent_count"], tools.RECENT_BOT_LIMIT)

    def test_truncation_is_declared(self):
        page = {"results": [{"id": BOT_ID, "join_at": "2030-01-01T00:00:00Z"}],
                "next": "https://us-east-1.recall.ai/api/v1/bot/?page=n"}
        with mock.patch.object(api, "request",
                               side_effect=[page] * api.MAX_LIST_PAGES + [{}]):
            result = json.loads(tools.list_bots({}))
        self.assertTrue(result["scheduled_truncated"])


# --------------------------------------------------------------------------
# M7 — cancel loses the race to dispatch
# --------------------------------------------------------------------------

class CancelTests(ApiTestCase):
    def test_405_on_delete_falls_through_to_leave_call(self):
        bot = {"id": BOT_ID, "join_at": "2030-01-01T00:00:00Z",
               "status_changes": [], "recordings": []}
        calls = []

        def fake_request(method, path, body=None, **kwargs):
            calls.append((method, path))
            if method == "GET":
                return bot
            if method == "DELETE":
                raise api.RecallHTTPError("HTTP 405 ...", 405)
            return {}

        with mock.patch.object(api, "request", side_effect=fake_request):
            result = json.loads(tools.cancel_bot({"bot_id": BOT_ID}))

        self.assertTrue(result["success"])
        self.assertEqual(result["action"], "removed_from_call")
        self.assertIn(("POST", f"/bot/{BOT_ID}/leave_call/"), calls)


# --------------------------------------------------------------------------
# M10 — bot ids reaching the URL and the filesystem
# --------------------------------------------------------------------------

class BotIdValidationTests(ApiTestCase):
    def test_valid_uuid_accepted(self):
        self.assertEqual(api.validate_bot_id(f"  {BOT_ID}  "), BOT_ID)

    def test_hostile_ids_rejected_before_any_request(self):
        for bad in ("../../etc/passwd", "abc?x=1", "abc#frag", "", None,
                    "3f2a1b4c5d6e4f809a1b2c3d4e5f6071"):
            with self.subTest(bad=bad):
                with mock.patch.object(api, "request") as request:
                    result = json.loads(tools.bot_status({"bot_id": bad}))
                self.assertFalse(result["success"])
                self.assertEqual(result["kind"], "arguments")
                request.assert_not_called()


# --------------------------------------------------------------------------
# to_readable — the one piece ported from someone else's implementation
# --------------------------------------------------------------------------

class ToReadableTests(unittest.TestCase):
    @staticmethod
    def _word(text, start=None, end=None):
        word = {"text": text}
        if start is not None:
            word["start_timestamp"] = {"relative": start}
        if end is not None:
            word["end_timestamp"] = {"relative": end}
        return word

    def test_consecutive_same_speaker_merges(self):
        parts = [
            {"participant": {"id": 1, "name": "Ada"},
             "words": [self._word("one", 0, 1)]},
            {"participant": {"id": 1, "name": "Ada"},
             "words": [self._word("two", 1, 2)]},
            {"participant": {"id": 2, "name": "Bob"},
             "words": [self._word("three", 2, 3)]},
        ]
        out = tx.to_readable(parts)
        self.assertEqual([item["speaker"] for item in out], ["Ada", "Bob"])
        self.assertEqual(out[0]["text"], "one two")
        self.assertEqual((out[0]["start"], out[0]["end"]), (0, 2))

    def test_entries_without_name_or_words_are_dropped(self):
        parts = [
            {"participant": {"id": 1}, "words": [self._word("x", 0, 1)]},
            {"participant": {"id": 2, "name": "Bob"}, "words": []},
            {"participant": {"id": 3, "name": "Cyd"},
             "words": [self._word("kept", 0, 1)]},
        ]
        self.assertEqual([i["speaker"] for i in tx.to_readable(parts)], ["Cyd"])

    def test_missing_timestamps_do_not_raise(self):
        parts = [{"participant": {"id": 1, "name": "Ada"},
                  "words": [self._word("no times")]}]
        out = tx.to_readable(parts)
        self.assertIsNone(out[0]["start"])
        self.assertEqual(tx.timecode(out[0]["start"]), "--:--:--")

    def test_empty_input(self):
        self.assertEqual(tx.to_readable([]), [])
        self.assertEqual(tx.to_readable(None), [])

    def test_speakers_are_sorted_and_unique(self):
        readable = [{"speaker": "Bob"}, {"speaker": "Ada"}, {"speaker": "Bob"}]
        self.assertEqual(tx.speakers(readable), ["Ada", "Bob"])


# --------------------------------------------------------------------------
# The handler contract every tool claims to honour
# --------------------------------------------------------------------------

class HandlerContractTests(ApiTestCase):
    ARGS = {
        "recall_schedule_bot": {"meeting_url": "https://zoom.us/j/1"},
        "recall_bot_status": {"bot_id": BOT_ID},
        "recall_list_bots": {},
        "recall_get_transcript": {"bot_id": BOT_ID},
        "recall_cancel_bot": {"bot_id": BOT_ID},
    }

    def test_no_handler_ever_raises_and_all_return_json(self):
        failures = [
            api.RecallError("api down"),
            api.RecallConfigError("bad config"),
            api.RecallArgumentError("bad argument"),
            api.RecallHTTPError("HTTP 500 ...", 500),
            TimeoutError("timed out"),
            KeyError("join_at"),
            ValueError("nope"),
            RuntimeError("unexpected"),
        ]
        for name, handler in agent_recall.HANDLERS.items():
            for failure in failures:
                with self.subTest(tool=name, failure=type(failure).__name__):
                    with mock.patch.object(api, "request", side_effect=failure):
                        raw = handler(self.ARGS[name])
                    payload = json.loads(raw)
                    self.assertIsInstance(payload["success"], bool)
                    self.assertFalse(payload["success"])
                    self.assertTrue(payload["error"])

    def test_dispatch_rejects_unknown_names_without_raising(self):
        payload = json.loads(agent_recall.dispatch("nope", {}))
        self.assertFalse(payload["success"])

    def test_every_schema_matches_its_handler(self):
        self.assertEqual(sorted(agent_recall.HANDLERS),
                         sorted(t["name"] for t in agent_recall.TOOLS))
        for tool in agent_recall.TOOLS:
            with self.subTest(tool=tool["name"]):
                self.assertEqual(set(tool), {"name", "description", "parameters"})
                self.assertEqual(tool["parameters"]["type"], "object")


# --------------------------------------------------------------------------
# MCP protocol, driven through main() over real streams
# --------------------------------------------------------------------------

class McpProtocolTests(ApiTestCase):
    def drive(self, *messages):
        lines = "\n".join(
            m if isinstance(m, str) else json.dumps(m) for m in messages) + "\n"
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(lines)), \
                mock.patch.object(sys, "stdout", out):
            code = mcp_server.main()
        self.assertEqual(code, 0)
        return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]

    def test_initialize_negotiation(self):
        known, unknown, absent = self.drive(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05"}},
            {"jsonrpc": "2.0", "id": 2, "method": "initialize",
             "params": {"protocolVersion": "1999-01-01"}},
            {"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {}},
        )
        self.assertEqual(known["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual(unknown["result"]["protocolVersion"],
                         mcp_server.PROTOCOL_VERSIONS[0])
        self.assertEqual(absent["result"]["protocolVersion"],
                         mcp_server.PROTOCOL_VERSIONS[0])
        self.assertIn("tools", known["result"]["capabilities"])
        self.assertEqual(known["result"]["serverInfo"]["version"],
                         agent_recall.__version__)

    def test_notifications_get_no_reply(self):
        replies = self.drive(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "method": "notifications/cancelled",
             "params": {"requestId": 1}},
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        )
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["id"], 1)

    def test_tools_list_uses_mcp_schema_key(self):
        reply, = self.drive({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        listed = reply["result"]["tools"]
        self.assertEqual(len(listed), len(agent_recall.TOOLS))
        for tool in listed:
            self.assertEqual(set(tool), {"name", "description", "inputSchema"})

    def test_unknown_tool_is_a_protocol_error(self):
        """L1: the spec reserves isError for execution failures."""
        reply, = self.drive({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                             "params": {"name": "nope", "arguments": {}}})
        self.assertEqual(reply["error"]["code"], -32602)

    def test_tool_failure_is_a_result_with_isError(self):
        reply, = self.drive({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                             "params": {"name": "recall_bot_status",
                                        "arguments": {}}})
        self.assertTrue(reply["result"]["isError"])
        self.assertFalse(json.loads(reply["result"]["content"][0]["text"])["success"])

    def test_malformed_and_invalid_messages(self):
        parse, batch, null_id, method = self.drive(
            "{ not json",
            [{"jsonrpc": "2.0", "id": 1, "method": "ping"}],
            {"jsonrpc": "2.0", "id": None, "method": "ping"},
            {"jsonrpc": "2.0", "id": 4, "method": "resources/list"},
        )
        self.assertEqual(parse["error"]["code"], -32700)
        self.assertEqual(batch["error"]["code"], -32600)
        self.assertEqual(null_id["error"]["code"], -32600)   # L2
        self.assertEqual(method["error"]["code"], -32601)

    def test_nothing_but_jsonrpc_reaches_stdout(self):
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(
                '{"jsonrpc":"2.0","id":1,"method":"ping"}\n')), \
                mock.patch.object(sys, "stdout", out), \
                mock.patch.object(sys, "stderr", io.StringIO()), \
                mock.patch.dict(os.environ, {"RECALL_REGION": "", "RECALL_API_KEY": ""}):
            mcp_server.main()
        for line in out.getvalue().splitlines():
            self.assertEqual(json.loads(line)["jsonrpc"], "2.0")

    def test_final_line_without_newline_is_answered(self):
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(
                '{"jsonrpc":"2.0","id":1,"method":"ping"}')), \
                mock.patch.object(sys, "stdout", out):
            mcp_server.main()
        self.assertEqual(json.loads(out.getvalue())["id"], 1)


# --------------------------------------------------------------------------
# Transcripts come back inline, paged, and touch disk only when asked to
# --------------------------------------------------------------------------

class InlineTranscriptTests(ApiTestCase):
    BOT = {
        "id": BOT_ID,
        "status_changes": [{"code": "done"}],
        "recordings": [{"id": "rec", "media_shortcuts": {"transcript": {
            "id": "tr", "data": {"download_url": "https://example.test/t.json"}}}}],
    }

    @staticmethod
    def parts(count):
        # Alternating speakers, so every entry stays its own turn.
        return [{"participant": {"id": i % 2, "name": f"Speaker {i % 2}"},
                 "words": [{"text": f"line {i}", "start_timestamp": {"relative": i}}]}
                for i in range(count)]

    def fetch(self, count=5, **args):
        with mock.patch.object(api, "request", return_value=self.BOT), \
                mock.patch.object(api, "download", return_value=self.parts(count)):
            return json.loads(tools.get_transcript({"bot_id": BOT_ID, **args}))

    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {"RECALL_OUTPUT_DIR": ""})
        env.start()
        self.addCleanup(env.stop)

    def test_full_text_is_inline(self):
        result = self.fetch()
        self.assertEqual(result["total_utterances"], 5)
        self.assertFalse(result["has_more"])
        self.assertIsNone(result["next_offset"])
        self.assertEqual(result["transcript"].count("Speaker"), 5)
        self.assertIn("[00:00:04] Speaker 0: line 4", result["transcript"])

    def test_pages_cover_every_turn_exactly_once(self):
        seen, offset = [], 0
        while offset is not None:
            page = self.fetch(offset=offset, limit=2)
            seen += page["transcript"].split("\n\n")
            offset = page["next_offset"]
            self.assertEqual(page["has_more"], offset is not None)
        self.assertEqual(seen, [f"[00:00:0{i}] Speaker {i % 2}: line {i}" for i in range(5)])

    def test_offset_past_the_end_is_an_empty_page(self):
        result = self.fetch(offset=99)
        self.assertTrue(result["success"])
        self.assertEqual(result["transcript"], "")
        self.assertFalse(result["has_more"])

    def test_junk_paging_arguments_fall_back_to_defaults(self):
        result = self.fetch(offset="x", limit=-3)
        self.assertEqual(result["offset"], 0)
        self.assertEqual(result["limit"], 1)

    def test_nothing_is_written_without_an_output_dir(self):
        with mock.patch.object(Path, "write_text") as write:
            result = self.fetch()
        write.assert_not_called()
        self.assertNotIn("transcript_path", result)

    def test_output_dir_opts_in_to_files(self):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        with mock.patch.dict(os.environ, {"RECALL_OUTPUT_DIR": directory}):
            result = self.fetch()
        self.assertTrue(Path(result["transcript_path"]).is_file())
        self.assertTrue(Path(result["raw_path"]).is_file())


# --------------------------------------------------------------------------
# Per-request credentials
# --------------------------------------------------------------------------

class CredentialOverrideTests(ApiTestCase):
    def test_override_wins_and_is_undone(self):
        with api.credentials(RECALL_API_KEY="caller", RECALL_REGION=None):
            self.assertEqual(api.api_key(), "caller")
            self.assertEqual(api.region(), "us-east-1")  # env fills the gap
        self.assertEqual(api.api_key(), "test-key")

    def test_concurrent_requests_see_only_their_own_key(self):
        barrier = threading.Barrier(2)
        seen = {}

        def run(key):
            with api.credentials(RECALL_API_KEY=key):
                barrier.wait()  # both overrides are live at once
                seen[key] = api.api_key()

        threads = [threading.Thread(target=run, args=(k,)) for k in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(seen, {"a": "a", "b": "b"})


# --------------------------------------------------------------------------
# Streamable HTTP transport, driven over a real socket
# --------------------------------------------------------------------------

class HttpTransportTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.server = http_server.build_server("127.0.0.1", 0)
        quiet = mock.patch.object(http_server.McpHandler, "log_message", lambda *a: None)
        quiet.start()
        self.addCleanup(quiet.stop)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

        # Recall itself is faked at urlopen, so the test sees the request the
        # server would really have sent.
        self.sent = []

        def fake_urlopen(req, timeout=None):
            self.sent.append(req)
            return FakeResponse(json.dumps({"id": BOT_ID, "status_changes": []}).encode())

        patcher = mock.patch.object(api.urllib.request, "urlopen", fake_urlopen)
        patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, body, headers=None, path="/mcp", method="POST"):
        if not isinstance(body, (str, bytes)):
            body = json.dumps(body)
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        self.addCleanup(conn.close)
        conn.request(method, path, body=body if method == "POST" else None,
                     headers={"Content-Type": "application/json", **(headers or {})})
        response = conn.getresponse()
        return response, response.read()

    def authed(self, body, **headers):
        return self.post(body, {"Authorization": "Bearer caller-key", **headers})

    STATUS_CALL = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                   "params": {"name": "recall_bot_status", "arguments": {"bot_id": BOT_ID}}}

    def test_missing_key_is_refused_even_when_the_server_has_one(self):
        for headers in ({}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer "}):
            with self.subTest(headers=headers):
                response, _ = self.post(self.STATUS_CALL, headers)
                self.assertEqual(response.status, 401)
                self.assertEqual(response.getheader("WWW-Authenticate"), "Bearer")
        self.assertEqual(self.sent, [])

    def test_initialize(self):
        response, body = self.authed({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                      "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "application/json")
        self.assertEqual(json.loads(body)["result"]["protocolVersion"], "2025-06-18")

    def test_the_callers_key_reaches_recall(self):
        response, body = self.authed(self.STATUS_CALL)
        self.assertEqual(response.status, 200)
        self.assertFalse(json.loads(body)["result"]["isError"])
        req, = self.sent
        self.assertEqual(req.get_header("Authorization"), "Token caller-key")
        self.assertTrue(req.full_url.startswith("https://us-east-1.recall.ai/"))

    def test_region_header_selects_the_region(self):
        self.authed(self.STATUS_CALL, **{"X-Recall-Region": "eu-central-1"})
        req, = self.sent
        self.assertTrue(req.full_url.startswith("https://eu-central-1.recall.ai/"))

    def test_hostile_region_header_never_becomes_a_url(self):
        _, body = self.authed(self.STATUS_CALL, **{"X-Recall-Region": "evil.test/x"})
        self.assertTrue(json.loads(body)["result"]["isError"])
        self.assertEqual(self.sent, [])

    def test_notification_is_accepted_without_a_body(self):
        response, body = self.authed({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(response.status, 202)
        self.assertEqual(body, b"")

    def test_client_response_is_accepted_without_a_body(self):
        response, body = self.authed({"jsonrpc": "2.0", "id": 3, "result": {}})
        self.assertEqual(response.status, 202)
        self.assertEqual(body, b"")

    def test_unparseable_body_is_a_400_with_a_jsonrpc_error(self):
        response, body = self.authed("{ not json")
        self.assertEqual(response.status, 400)
        self.assertEqual(json.loads(body)["error"]["code"], -32700)

    def test_get_and_delete_are_not_offered(self):
        for method in ("GET", "DELETE"):
            with self.subTest(method=method):
                response, _ = self.post(None, {"Authorization": "Bearer k"}, method=method)
                self.assertEqual(response.status, 405)

    def test_other_paths_are_not_found(self):
        response, _ = self.post({"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                {"Authorization": "Bearer k"}, path="/")
        self.assertEqual(response.status, 404)

    def test_oversized_body_is_refused_unread(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        self.addCleanup(conn.close)
        conn.putrequest("POST", "/mcp")
        conn.putheader("Authorization", "Bearer k")
        conn.putheader("Content-Length", str(http_server.MAX_BODY_BYTES + 1))
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 413)

    def test_keep_alive_serves_several_requests(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        self.addCleanup(conn.close)
        for msg_id in (1, 2):
            conn.request("POST", "/mcp", body=json.dumps(
                {"jsonrpc": "2.0", "id": msg_id, "method": "ping"}),
                headers={"Authorization": "Bearer k"})
            response = conn.getresponse()
            self.assertEqual(json.loads(response.read())["id"], msg_id)


# --------------------------------------------------------------------------
# Packaging and manifests — C1/C2 and the version that lives in two files
# --------------------------------------------------------------------------

class ManifestTests(unittest.TestCase):
    def test_mcp_manifest_uses_the_console_script(self):
        """C1/C2: `python3 -m` put the user's CWD on sys.path, and the
        PYTHONPATH placeholders never resolved under Grok."""
        config = json.loads((REPO / ".mcp.json").read_text())
        server = config["mcpServers"]["recall"]
        self.assertEqual(server["command"], "agent-recall-mcp")
        self.assertNotIn("PYTHONPATH", server.get("env", {}))
        self.assertNotIn("-m", server.get("args", []))

    def test_plugin_version_matches_the_package(self):
        """L7: two hand-maintained copies of one version number."""
        manifest = json.loads((REPO / ".claude-plugin/plugin.json").read_text())
        self.assertEqual(manifest["version"], agent_recall.__version__)

    def test_plugin_prompts_for_credentials(self):
        """M9: deleting plugin.yaml dropped its requires_env declarations."""
        manifest = json.loads((REPO / ".claude-plugin/plugin.json").read_text())
        self.assertEqual(set(manifest["userConfig"]),
                         {"recall_api_key", "recall_region"})
        self.assertTrue(manifest["userConfig"]["recall_api_key"]["sensitive"])

    def test_bundled_skill_is_named_recall(self):
        skill = REPO / "skills/recall/SKILL.md"
        self.assertTrue(skill.is_file(), "skills/<name>/SKILL.md is where hosts look")
        self.assertIn("name: recall", skill.read_text(encoding="utf-8"))

    def test_license_file_exists(self):
        """M11: an MIT declaration with no license text."""
        self.assertIn("MIT License", (REPO / "LICENSE").read_text())

    def test_host_supplied_plugin_options_are_honoured(self):
        """M9: userConfig answers arrive as CLAUDE_PLUGIN_OPTION_*."""
        with mock.patch.dict(os.environ, {"RECALL_API_KEY": "",
                                          "CLAUDE_PLUGIN_OPTION_RECALL_API_KEY": "k"}):
            self.assertEqual(api.api_key(), "k")

    def test_explicit_env_wins_over_plugin_options(self):
        with mock.patch.dict(os.environ, {"RECALL_API_KEY": "explicit",
                                          "CLAUDE_PLUGIN_OPTION_RECALL_API_KEY": "k"}):
            self.assertEqual(api.api_key(), "explicit")


if __name__ == "__main__":
    unittest.main()
