import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from export_ai_providers import collect, safe_label, update_provider_section
from wakapi_ai_stats import normalize_layout, render_providers


class ExportProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.now = datetime(2026, 11, 14, 10, tzinfo=timezone.utc)
        self.when = "2026-11-14T09:00:00Z"

    def log(self, path, rows):
        target = self.home / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(json.dumps(row) for row in rows))

    def pi_rows(self):
        return [
            {"type": "session", "id": "private-session-id", "cwd": "private-project"},
            {"type": "message", "id": "private-record-id", "timestamp": self.when,
             "message": {"role": "assistant", "responseId": "private-response-id",
                         "provider": "openai-codex", "model": "gpt-example",
                         "content": "PRIVATE CONVERSATION sk-secret-do-not-export",
                         "usage": {"input": 100, "output": 20, "cacheRead": 50,
                                   "cacheWrite": 5, "cost": {"total": 999}}}},
        ]

    def test_pi_exports_only_aggregate_metadata_and_deduplicates_forks(self):
        self.log(".pi/agent/sessions/a/main.jsonl", self.pi_rows())
        self.log(".pi/agent/sessions/a/fork.jsonl", self.pi_rows())
        snapshot = collect(self.home, self.now)
        self.assertEqual(snapshot["rows"], [{
            "agent": "pi", "provider": "openai-codex", "model": "gpt-example",
            "input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 50,
            "cache_write_tokens": 5, "responses": 1, "sessions": 1,
        }])
        exported = json.dumps(snapshot)
        for secret in ["private-session", "private-record", "private-response", "private-project",
                       "PRIVATE CONVERSATION", "sk-secret", "cost", str(self.home)]:
            self.assertNotIn(secret, exported)

    def test_codex_uses_response_delta_not_cumulative_totals(self):
        self.log(".codex/sessions/a.jsonl", [
            {"type": "session_meta", "payload": {"id": "session", "model_provider": "custom-gateway"}},
            {"type": "turn_context", "payload": {"turn_id": "turn", "model": "gpt-example"}},
            {"type": "token_usage_record", "timestamp": self.when,
             "payload": {"response_id": "r1", "turn_id": "turn",
                         "usage": {"input_tokens": 100, "cached_input_tokens": 40, "output_tokens": 10},
                         "thread_token_usage": {"input_tokens": 999999}}},
            {"type": "token_usage_record", "timestamp": self.when,
             "payload": {"response_id": "r2", "turn_id": "turn",
                         "usage": {"input_tokens": 200, "cached_input_tokens": 50, "output_tokens": 20}}},
        ])
        row = collect(self.home, self.now)["rows"][0]
        self.assertEqual(row["provider"], "custom-gateway")
        self.assertEqual(row["input_tokens"], 210)
        self.assertEqual(row["cache_read_tokens"], 90)
        self.assertEqual(row["output_tokens"], 30)
        self.assertEqual(row["sessions"], 1)

    def test_claude_does_not_infer_provider_from_model(self):
        self.log(".claude/projects/a.jsonl", [
            {"type": "assistant", "timestamp": self.when, "sessionId": "private",
             "message": {"id": "r1", "model": "claude-example", "usage": {"input_tokens": 10}}},
            {"type": "assistant", "timestamp": self.when,
             "message": {"model": "<synthetic>", "usage": {"input_tokens": 999}}},
        ])
        rows = collect(self.home, self.now)["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["provider"], "unknown")
        self.assertEqual(rows[0]["input_tokens"], 10)

    def test_old_and_empty_usage_records_are_excluded(self):
        rows = self.pi_rows()
        rows[1]["timestamp"] = "2026-10-01T00:00:00Z"
        self.log(".pi/agent/sessions/old.jsonl", rows)
        self.assertEqual(collect(self.home, self.now)["rows"], [])

    def test_safe_labels_reject_urls_credentials_and_markdown(self):
        for value in ["https://gateway/api?key=secret", "sk-secret", "waka_secret", "bad|row", None]:
            self.assertEqual(safe_label(value), "unknown")

    def test_rendering_preserves_other_sections_and_keeps_date_last(self):
        self.log(".pi/agent/sessions/a.jsonl", self.pi_rows())
        snapshot = collect(self.home, self.now)
        readme = ("<!--START_SECTION:ai-coding-->Wakapi lines<!--END_SECTION:ai-coding-->\n\n"
                  "<!--START_SECTION:ai-providers-->old<!--END_SECTION:ai-providers-->\n\n"
                  " Last Updated on 13 November 2026 UTC\n\ncontact")
        result = normalize_layout(update_provider_section(readme, snapshot), "14 November 2026")
        self.assertIn("Wakapi lines", result)
        self.assertIn("openai-codex | gpt-example | pi | 175 | 1", result)
        self.assertGreater(result.index("Last Updated"), result.index("END_SECTION:ai-providers"))
        self.assertEqual(result.count("Last Updated"), 1)
        self.assertEqual(update_provider_section(result, snapshot), result)
        self.assertIn("Separate from Wakapi", render_providers(snapshot))


if __name__ == "__main__":
    unittest.main()
