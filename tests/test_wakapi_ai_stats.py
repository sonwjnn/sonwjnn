from __future__ import annotations

import base64
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from wakapi_ai_stats import (
    _api_base,
    _get_json,
    _fetch_week,
    aggregate,
    render,
    replace_section,
    normalize_layout,
    reported_model,
)


class WakapiAiStatsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.start = datetime(2026, 11, 7, tzinfo=timezone.utc)
        self.end = datetime(2026, 11, 14, tzinfo=timezone.utc)
        self.stats = {
            "timezone": "UTC",
            "categories": [
                {"text": "AI Coding", "total_seconds": 7200, "percent": 25.0}
            ],
        }
        self.heartbeats = [
            {
                "time": self.start.timestamp() + 10,
                "ai_model": "claude-sonnet-4",
                "ai_session": "session-1",
                "ai_line_changes": 10,
                "human_line_changes": 2,
                "ai_input_tokens": 1000,
                "ai_output_tokens": 400,
                "ai_prompt_length": 50,
            },
            {
                "time": self.start.timestamp() + 20,
                "ai_model": "claude-sonnet-4",
                "ai_session": "session-1",
                "ai_line_changes": 5,
                "human_line_changes": 0,
                "ai_input_tokens": 500,
                "ai_output_tokens": 200,
                "ai_prompt_length": 30,
            },
            {
                "time": self.start.timestamp() + 30,
                "human_line_changes": 3,
            },
            {
                # Outside the stats window; this must be ignored.
                "time": self.end.timestamp() + 1,
                "ai_session": "future-session",
                "ai_input_tokens": 999,
            },
        ]

    def test_recovers_model_version_from_wakapi_raw_user_agent(self) -> None:
        self.assertEqual(reported_model({
            "ai_model": "Gpt",
            "user_agent_id": "wakatime/1 (darwin) gpt/5.2 codex/1.0",
        }), "gpt/5.2")
        self.assertEqual(reported_model({
            "ai_model": "Deepseek",
            "user_agent_id": "wakatime/1 (darwin) deepseek/v3.2 pi/1.0",
        }), "deepseek/v3.2")

    def test_does_not_invent_model_version_or_provider(self) -> None:
        for heartbeat in [
            {"ai_model": "Gpt", "user_agent_id": "opaque-id"},
            {"ai_model": "Gpt", "user_agent_id": "wakatime/1 codex/5.2"},
            {"ai_model": "Gpt", "user_agent_id": "gpt/unknown"},
        ]:
            self.assertEqual(reported_model(heartbeat), "Gpt")
        self.assertEqual(reported_model({"user_agent_id": "gpt/5.2"}), "")
        self.assertEqual(reported_model({"ai_model": "openrouter/openai/gpt-5.2"}),
                         "openrouter/openai/gpt-5.2")

    def test_aggregation_separates_versions_in_same_model_family(self) -> None:
        heartbeats = [dict(event) for event in self.heartbeats[:2]]
        for version, event in zip(["4.5", "4.6"], heartbeats):
            event["ai_model"] = "Sonnet"
            event["user_agent_id"] = f"wakatime/1 sonnet/{version} claude-code/1"
        result = aggregate(self.stats, heartbeats, self.start, self.end)
        self.assertEqual(set(result["models"]), {"sonnet/4.5", "sonnet/4.6"})
        self.assertEqual(result["models"]["sonnet/4.5"]["input_tokens"], 1000)
        self.assertEqual(result["models"]["sonnet/4.6"]["input_tokens"], 500)
        self.assertEqual(result["sessions"], 1)

    def test_aggregates_weekly_heartbeat_fields(self) -> None:
        result = aggregate(self.stats, self.heartbeats, self.start, self.end)

        self.assertEqual(result["ai_seconds"], 7200)
        self.assertEqual(result["ai_percent"], 25.0)
        self.assertEqual(result["ai_lines"], 15)
        self.assertEqual(result["human_lines"], 5)
        self.assertEqual(result["input_tokens"], 1500)
        self.assertEqual(result["output_tokens"], 600)
        self.assertEqual(result["sessions"], 1)
        self.assertEqual(result["prompt_events"], 2)
        self.assertEqual(result["ai_event_count"], 2)

    def test_render_shows_real_fields_but_not_cost(self) -> None:
        result = aggregate(self.stats, self.heartbeats, self.start, self.end)
        output = render(result)

        self.assertIn("AI Coding This Week", output)
        self.assertIn("⏱ AI Coding Time: 2 hrs 0 mins (25.00%)", output)
        self.assertIn("✍️ Lines changed: 20", output)
        self.assertNotIn("AI 15 · human 5", output)
        self.assertIn("1,500 Input Tokens, 600 Output Tokens", output)
        self.assertIn("█████████████████████████  100.00 %", output)
        self.assertIn("15 lines", output)
        self.assertNotIn("cost", output.casefold())

    def test_layout_has_one_title_and_date_below_ai(self) -> None:
        readme = (
            "<!--START_SECTION:waka-->stats\n Last Updated on 13 November 2026 UTC\n"
            "<!--END_SECTION:waka-->\n\n### AI coding activity\n\n"
            "<!--START_SECTION:ai-coding-->AI content<!--END_SECTION:ai-coding-->\n\n"
            "### Get in touch\nlinks\n"
        )
        result = normalize_layout(readme, "14 November 2026")
        self.assertNotIn("### AI coding activity", result)
        self.assertEqual(result.count("Last Updated on"), 1)
        self.assertLess(result.index("<!--END_SECTION:ai-coding-->"), result.index("Last Updated"))
        self.assertLess(result.index("Last Updated"), result.index("### Get in touch"))
        self.assertEqual(normalize_layout(result, "14 November 2026"), result)

    def test_ai_section_moves_above_weekly_stats_and_date_stays_below_both(self) -> None:
        ai = ('<!--START_SECTION:ai-coding-->\nAI summary\n<!--END_SECTION:ai-coding-->\n\n'
              '<!--START_SECTION:ai-providers-->\n<!--END_SECTION:ai-providers-->')
        waka = ('<!--START_SECTION:waka-->\nThis Week I Spent My Time On\n'
                '```text\nPython 5 hrs\n```\n<!--END_SECTION:waka-->')
        readme = 'Intro\n\n### Coding activity\n\n' + waka + '\n\n' + ai + '\n\nContact\n'
        updated = normalize_layout(readme, '14 November 2026')
        self.assertTrue(updated.startswith('Intro\n\n### Coding activity\n\n'))
        self.assertIn(ai, updated)
        self.assertIn(waka, updated)
        self.assertLess(updated.index(ai), updated.index(waka))
        self.assertLess(updated.index('<!--END_SECTION:waka-->'), updated.index('Last Updated'))
        self.assertLess(updated.index('Last Updated'), updated.index('Contact'))
        for marker in ['ai-coding', 'ai-providers', 'waka']:
            self.assertEqual(updated.count(f'<!--START_SECTION:{marker}-->'), 1)
            self.assertEqual(updated.count(f'<!--END_SECTION:{marker}-->'), 1)
        self.assertEqual(normalize_layout(updated, '14 November 2026'), updated)

    def test_layout_removes_commit_schedule_sections_but_preserves_other_stats(self) -> None:
        for chronotype in ["**I'm an Early 🐤**", "**I'm a Night 🦉**"]:
            with self.subTest(chronotype=chronotype):
                readme = (
                    "<!--START_SECTION:waka-->\n" + chronotype + " \n\n"
                    "```text\n🌞 Morning 100 commits\n🌙 Night 50 commits\n```\n"
                    "📅 **I'm Most Productive on Wednesday** \n\n"
                    "```text\nMonday 100 commits\nWednesday 200 commits\n```\n\n"
                    "**This Week I Spent My Time On**\n\n```text\nPython 5 hrs\n```\n"
                    "<!--END_SECTION:waka-->\n"
                    "<!--START_SECTION:ai-coding-->AI content<!--END_SECTION:ai-coding-->\n"
                )
                updated = normalize_layout(readme, "14 November 2026")
                self.assertNotIn("I'm an Early", updated)
                self.assertNotIn("I'm a Night", updated)
                self.assertNotIn("Most Productive", updated)
                self.assertNotIn("Morning 100", updated)
                self.assertNotIn("Wednesday 200", updated)
                self.assertIn("Python 5 hrs", updated)
                self.assertIn("<!--START_SECTION:waka-->", updated)
                self.assertIn("<!--END_SECTION:waka-->", updated)
                self.assertEqual(normalize_layout(updated, "14 November 2026"), updated)

    def test_empty_models_are_hidden_and_bars_use_line_share(self) -> None:
        result = aggregate(self.stats, self.heartbeats, self.start, self.end)
        result["models"]["Unknown"] = {
            "ai_lines": 0, "input_tokens": 0, "output_tokens": 0, "events": 1
        }
        output = render(result)
        self.assertNotIn("Unknown", output)
        self.assertIn("100.00 %", output)
        self.assertIn("\n\n🧠", output)

    def test_empty_week_gets_clear_fallback(self) -> None:
        stats = {"categories": []}
        result = aggregate(stats, [], self.start, self.end)

        self.assertIn("No AI coding telemetry", render(result))

    def test_fetch_week_uses_stats_range_and_user_timezone(self) -> None:
        stats = {
            "start": "2026-11-07T00:00:00+07:00",
            "end": "2026-11-14T00:00:00+07:00",
            "timezone": "Asia/Saigon",
            "categories": [],
        }
        calls = []

        def fake_get_json(url: str, _api_key: str) -> dict:
            calls.append(url)
            if url.endswith("/stats/last_7_days"):
                return {"data": stats}
            return {"data": []}

        with patch("wakapi_ai_stats._get_json", side_effect=fake_get_json):
            _, heartbeats, start, end = _fetch_week(
                "https://wakapi.dev/api/compat/wakatime/v1", "key"
            )

        self.assertEqual(heartbeats, [])
        self.assertEqual(start.utcoffset().total_seconds(), 7 * 60 * 60)
        self.assertEqual(end.utcoffset().total_seconds(), 7 * 60 * 60)
        self.assertEqual(len(calls), 9)  # Stats request plus Nov 7 through Nov 14.
        self.assertIn("date=2026-11-07", calls[1])
        self.assertIn("date=2026-11-14", calls[-1])

    def test_api_base_accepts_stats_or_heartbeat_base(self) -> None:
        self.assertEqual(
            _api_base("https://wakapi.dev/api/compat/wakatime/v1/"),
            "https://wakapi.dev/api/compat/wakatime/v1",
        )
        self.assertEqual(
            _api_base("https://wakapi.dev/api/"),
            "https://wakapi.dev/api/compat/wakatime/v1",
        )

    def test_api_key_is_sent_as_encoded_basic_auth_not_in_url(self) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b'{"data": {}}'

        with patch("wakapi_ai_stats.urlopen", return_value=Response()) as mocked:
            self.assertEqual(_get_json("https://wakapi.dev/test", "secret-key"), {"data": {}})

        request = mocked.call_args.args[0]
        self.assertNotIn("secret-key", request.full_url)
        encoded = request.get_header("Authorization").split(" ", 1)[1]
        self.assertEqual(base64.b64decode(encoded).decode(), "secret-key")

    def test_replace_section_only_changes_ai_marker_content(self) -> None:
        readme = (
            "before\n<!--START_SECTION:waka-->old stats<!--END_SECTION:waka-->\n"
            "<!--START_SECTION:ai-coding-->old ai<!--END_SECTION:ai-coding-->\nafter"
        )
        updated = replace_section(readme, "new ai stats\n")

        self.assertIn("<!--START_SECTION:waka-->old stats<!--END_SECTION:waka-->", updated)
        self.assertIn("<!--START_SECTION:ai-coding-->\nnew ai stats\n<!--END_SECTION:ai-coding-->", updated)
        self.assertTrue(updated.endswith("after"))


if __name__ == "__main__":
    unittest.main()
