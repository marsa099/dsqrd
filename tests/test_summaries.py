import json
import os
import tempfile
import threading
import time
import unittest
from typing import Any
from unittest import mock

import dsqrd as daemon


class FakeDiscord:
    def __init__(self, messages: list[dict[str, Any]]):
        self.messages = sorted(messages, key=lambda m: int(m["id"]), reverse=True)
        self.calls: list[tuple[Any, int, Any]] = []

    def get_messages(self, channel: Any, num: int = 100, before: Any = None):
        self.calls.append((channel, num, before))
        rows = self.messages
        if before is not None:
            rows = [m for m in rows if int(m["id"]) < int(before)]
        return rows[:num]


class FakeGateway:
    def __init__(self, read_id: str = "0"):
        self.read_id = read_id

    def get_read_state(self):
        return {"channel": {"last_acked_message_id": self.read_id}}


def message(mid: int, content: str = "message", **fields: Any) -> dict[str, Any]:
    value = {
        "id": str(mid),
        "timestamp": "2026-09-16T10:00:00+00:00",
        "content": content,
        "user_id": "user",
        "username": "David",
        "mentions": [],
        "embeds": [],
        "stickers": [],
    }
    value.update(fields)
    return value


def app_with(messages: list[dict[str, Any]], markers: dict[str, str] | None = None) -> Any:
    app: Any = object.__new__(daemon.DQS)
    app.discord = FakeDiscord(messages)
    app.gateway = FakeGateway()
    app.user_names = {}
    app.my_id = "reader"
    app.chan_name = {"channel": "general"}
    app._visit_markers = markers or {}
    app._visit_cutoffs = {}
    app._latest_messages = {}
    app._visit_lock = threading.Lock()
    app._summarize_cfg = lambda: {"base_url": "https://example.invalid"}
    app.events = []
    app.broadcast = app.events.append
    return app


class SummaryBehaviorTest(unittest.TestCase):
    def test_summary_pages_to_previous_participation_without_moving_the_boundary(self):
        rows = [message(mid) for mid in range(1, 451)]
        app = app_with(rows, {"channel": "100"})
        app._visit_cutoffs["channel"] = 100
        app.gateway = FakeGateway("400")
        app._summarize_transcript = lambda _cfg, lines, _scope, _topic="": f"summary of {len(lines)}"

        app.do_summarize("channel", "all_new", "")

        event = next(e for e in app.events if e["type"] == "summary")
        self.assertEqual(event["coverageCount"], 350)
        self.assertEqual(event["coverage"], "350 messages · 16 Sep 2026, 12:00")
        self.assertEqual(app._visit_cutoffs["channel"], 100)
        self.assertEqual(app._visit_markers["channel"], "100")
        self.assertEqual(len(app.discord.calls), 4)

    def test_participation_boundary_advances_from_own_messages_only(self):
        app = app_with([], {"channel": "100"})
        app._enter_visit("channel")
        self.assertEqual(app._visit_cutoffs["channel"], 100)
        app._note_latest("channel", message(150, user_id="someone-else"))
        app._note_latest("channel", message(140, user_id="reader"))

        with tempfile.TemporaryDirectory() as data, \
             mock.patch.object(daemon, "PARTICIPATION_MARKERS_JSON", os.path.join(data, "participation.json")):
            app._leave_visit("channel")
            app._enter_visit("channel")
            with open(daemon.PARTICIPATION_MARKERS_JSON) as f:
                persisted = json.load(f)

        self.assertEqual(persisted["channel"], "140")
        self.assertEqual(app._visit_cutoffs["channel"], 140)

    def test_first_visit_ignores_own_messages_from_the_last_two_minutes(self):
        now_ms = int(time.time() * 1000)
        recent = ((now_ms - 30_000 - daemon.DISCORD_EPOCH) << 22)
        previous = ((now_ms - 180_000 - daemon.DISCORD_EPOCH) << 22)
        app = app_with([], {"channel": str(recent)})
        app._enter_visit("channel")
        self.assertEqual(app._visit_cutoffs["channel"], 0)
        app._seed_participation_cutoff("channel", [
            message(recent, user_id="reader", content="I'm back"),
            message(recent - 1, user_id="someone-else"),
            message(previous, user_id="reader"),
        ])
        self.assertEqual(app._visit_cutoffs["channel"], previous)

    def test_failed_summary_does_not_change_participation_boundary(self):
        app = app_with([message(101)], {"channel": "100"})
        app._visit_cutoffs["channel"] = 100
        app._summarize_transcript = mock.Mock(side_effect=RuntimeError("provider failed"))

        app.do_summarize("channel", "all_new", "")

        self.assertEqual(app._visit_cutoffs["channel"], 100)
        self.assertEqual(app._visit_markers["channel"], "100")
        self.assertEqual(app.events[-1]["type"], "summaryError")

    def test_named_ranges_ignore_saved_participation_markers(self):
        for scope in ("session", "last_day"):
            with self.subTest(scope=scope):
                app = app_with([message(1), message(2), message(3)], {"channel": "2"})
                app._visit_cutoffs["channel"] = 2
                app._summarize_transcript = lambda _cfg, lines, _scope, _topic="": f"summary of {len(lines)}"

                with mock.patch.object(daemon.time, "time", return_value=daemon.DISCORD_EPOCH / 1000):
                    app.do_summarize("channel", scope, "")

                event = next(e for e in app.events if e["type"] == "summary")
                self.assertEqual(event["coverageCount"], 3)
                self.assertEqual(app._visit_markers["channel"], "2")

    def test_large_summary_uses_bounded_chunks_and_final_synthesis(self):
        app = app_with([])
        calls = []

        def llm_call(_cfg, _prompt, content):
            calls.append(("note", content))
            return "condensed notes"

        def llm_summary(_cfg, content, _scope):
            calls.append(("final", content))
            return "final summary"

        app._llm_call = llm_call
        app._llm_summarize = llm_summary
        result = app._summarize_transcript({}, ["x" * 9000, "y" * 9000, "z" * 9000], "last_week")

        self.assertEqual(result, "final summary")
        note_inputs = [content for kind, content in calls if kind == "note"]
        self.assertGreaterEqual(len(note_inputs), 3)
        self.assertTrue(all(len(content) <= daemon.SUMMARY_CHUNK_CHARS for content in note_inputs))
        self.assertTrue(calls[-1][1].startswith("Partial summaries:"))

    def test_topic_summary_uses_last_week_and_focuses_the_model(self):
        app = app_with([message(1)])
        captured = []

        def summarize(cfg, lines, scope, topic=""):
            captured.append((scope, topic, list(lines)))
            return "topic summary"

        app._summarize_transcript = summarize
        with mock.patch.object(daemon.time, "time", return_value=daemon.DISCORD_EPOCH / 1000):
            app.do_summarize("channel", "last_week", "", "pinecones")

        self.assertEqual(captured[0][0:2], ("last_week", "pinecones"))
        event = next(e for e in app.events if e["type"] == "summary")
        self.assertEqual(event["topic"], "pinecones")

    def test_chunked_topic_summary_focuses_notes_and_final_prompt(self):
        app = app_with([])
        prompts = []
        final_cfg = []
        app._llm_call = lambda _cfg, prompt, _content: prompts.append(prompt) or "notes"
        app._llm_summarize = lambda cfg, _content, _scope: final_cfg.append(cfg) or "summary"

        result = app._summarize_transcript({}, ["x" * 9000, "y" * 9000], "last_week", "pinecones")

        self.assertEqual(result, "summary")
        self.assertTrue(all("pinecones" in prompt for prompt in prompts))
        self.assertTrue(all("MENTIONS YOU" in prompt for prompt in prompts))
        self.assertEqual(final_cfg[0]["_summary_topic"], "pinecones")

    def test_direct_mentions_are_marked_high_priority(self):
        app = app_with([])
        line = app._summ_line_text(message(1, "can you review this?", mentions=[{"id": "reader"}]))
        self.assertEqual(line, "[MENTIONS YOU — HIGH PRIORITY] can you review this?")

    def test_attachment_only_messages_are_included_in_summary_transcript(self):
        row = message(
            1,
            content="",
            embeds=[
                {"type": "image/png", "name": "diagram.png"},
                {"type": "application/pdf", "name": "brief.pdf"},
                {"type": "audio/ogg", "name": "voice-message.ogg", "duration_secs": 12.4},
            ],
            stickers=[{"name": "party parrot"}],
        )
        app = app_with([row])
        captured = []
        app._summarize_transcript = lambda _cfg, lines, _scope, _topic="": captured.extend(lines) or "summary"

        app.do_summarize("channel", "session", "")

        self.assertEqual(len(captured), 1)
        self.assertIn("[image: diagram.png]", captured[0])
        self.assertIn("[application: brief.pdf]", captured[0])
        self.assertIn("[voice message: voice-message.ogg, 12s]", captured[0])
        self.assertIn("[sticker: party parrot]", captured[0])
        self.assertEqual(next(e for e in app.events if e["type"] == "summary")["coverageCount"], 1)


if __name__ == "__main__":
    unittest.main()
