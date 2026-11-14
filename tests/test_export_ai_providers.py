import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from export_ai_providers import collect, recorded_cost, safe_label, update_provider_section
from wakapi_ai_stats import LOCAL_USAGE_DIVIDER, normalize_layout, render_providers, update_readme


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
            "cost_usd": 999.0, "cost_responses": 1,
        }])
        exported = json.dumps(snapshot)
        for secret in ["private-session", "private-record", "private-response", "private-project",
                       "PRIVATE CONVERSATION", "sk-secret", str(self.home)]:
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
        self.assertIsNone(row["cost_usd"])
        self.assertEqual(row["cost_responses"], 0)

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
        self.assertIsNone(rows[0]["cost_usd"])

    def test_old_and_empty_usage_records_are_excluded(self):
        rows = self.pi_rows()
        rows[1]["timestamp"] = "2026-10-01T00:00:00Z"
        self.log(".pi/agent/sessions/old.jsonl", rows)
        self.assertEqual(collect(self.home, self.now)["rows"], [])

    def test_calendar_month_includes_older_than_seven_days_and_excludes_boundaries(self):
        rows = self.pi_rows()[:1]
        for i, when in enumerate(['2026-10-31T23:59:59Z', '2026-11-01T00:00:00Z',
                                  '2026-11-02T12:00:00Z', self.now.isoformat(),
                                  '2026-12-01T00:00:00Z']):
            event = self.pi_rows()[1]
            event['timestamp'] = when
            event['message']['responseId'] = f'response-{i}'
            rows.append(event)
        self.log('.pi/agent/sessions/month.jsonl', rows)
        snapshot = collect(self.home, self.now)
        self.assertEqual(snapshot['start'], '2026-11-01T00:00:00+00:00')
        self.assertEqual(snapshot['end'], self.now.isoformat())
        self.assertEqual(snapshot['rows'][0]['responses'], 2)
        self.assertEqual(snapshot['rows'][0]['input_tokens'], 200)
        self.assertEqual(snapshot['rows'][0]['cost_usd'], 1998)

    def test_month_start_uses_utc_even_when_now_has_a_local_offset(self):
        local_now = datetime(2026, 3, 1, 1, tzinfo=timezone(timedelta(hours=7)))
        snapshot = collect(self.home, local_now)
        self.assertEqual(snapshot['start'], '2026-02-01T00:00:00+00:00')
        self.assertEqual(snapshot['end'], '2026-02-28T18:00:00+00:00')

    def test_weekly_readme_migration_does_not_relabel_stale_wakapi_totals(self):
        snapshot = collect(self.home, self.now)
        readme = ('<!--START_SECTION:ai-coding-->🤖 **AI Coding This Week**\n\n'
                  '```text\n✍️ Lines changed: 99,999\n```\n<!--END_SECTION:ai-coding-->\n'
                  '<!--START_SECTION:ai-providers-->old<!--END_SECTION:ai-providers-->')
        updated = update_provider_section(readme, snapshot)
        self.assertIn('AI Coding This Month', updated)
        self.assertNotIn('AI Coding This Week', updated)
        self.assertNotIn('99,999', updated)
        self.assertNotIn('Lines changed:', updated)
        self.assertEqual(update_provider_section(updated, snapshot), updated)

    def test_recorded_cost_rejects_invalid_values_but_preserves_zero(self):
        for value in [None, True, False, -1, float('nan'), float('inf'), '1.20', {}]:
            with self.subTest(value=value):
                self.assertIsNone(recorded_cost({'cost': {'total': value}}))
        self.assertIsNone(recorded_cost({'cost': 12}))
        self.assertIsNone(recorded_cost({}))
        self.assertEqual(recorded_cost({'cost': {'total': 0}}), 0.0)
        self.assertEqual(recorded_cost({'cost': {'total': 0.12}}), 0.12)

    def test_partial_costs_keep_coverage_and_missing_is_not_zero(self):
        rows = self.pi_rows()
        second = json.loads(json.dumps(rows[1]))
        second['id'] = 'second'
        second['message']['responseId'] = 'second-response'
        del second['message']['usage']['cost']
        rows.append(second)
        self.log('.pi/agent/sessions/a.jsonl', rows)
        snapshot = collect(self.home, self.now)
        row = snapshot['rows'][0]
        self.assertEqual(row['cost_usd'], 999)
        self.assertEqual(row['cost_responses'], 1)
        self.assertEqual(row['responses'], 2)
        self.assertIn('Total: 350 tokens · $999.00* USD', render_providers(snapshot))
        visible = render_providers(snapshot).split('```', 1)[0]
        self.assertIn('Total: 350 tokens · $999.00* USD', visible)
        self.assertNotIn('Partial recorded cost;', visible)
        self.assertNotIn('missing costs are not zero', visible)
        self.assertNotIn('partial: 1/2', visible)

    def test_recorded_cost_sums_without_counting_duplicate_responses(self):
        rows = self.pi_rows()
        rows[1]['message']['usage']['cost']['total'] = 0.1
        second = json.loads(json.dumps(rows[1]))
        second['message']['responseId'] = 'second-response'
        second['message']['usage']['cost']['total'] = 0.2
        rows.extend([second, second])
        self.log('.pi/agent/sessions/a.jsonl', rows)
        row = collect(self.home, self.now)['rows'][0]
        self.assertEqual(row['cost_usd'], 0.3)
        self.assertEqual(row['cost_responses'], 2)
        self.assertEqual(row['responses'], 2)

    def test_empty_snapshot_and_zero_token_share(self):
        snapshot = collect(self.home, self.now)
        self.assertIn('No local session usage', render_providers(snapshot))
        snapshot['rows'] = [{'provider': 'p', 'model': 'm', 'agent': 'pi'}]
        self.assertIn('0.0%', render_providers(snapshot))
        self.assertIn('Total: 0 tokens · N/A USD', render_providers(snapshot))

    def test_missing_cost_is_null_and_rendered_na(self):
        rows = self.pi_rows()
        del rows[1]['message']['usage']['cost']
        self.log('.pi/agent/sessions/a.jsonl', rows)
        snapshot = collect(self.home, self.now)
        self.assertIsNone(snapshot['rows'][0]['cost_usd'])
        self.assertEqual(snapshot['rows'][0]['cost_responses'], 0)
        self.assertIn('Total: 175 tokens · N/A USD', render_providers(snapshot))

    def test_zero_cost_and_legacy_snapshots(self):
        self.log('.pi/agent/sessions/a.jsonl', self.pi_rows())
        snapshot = collect(self.home, self.now)
        snapshot['rows'][0]['cost_usd'] = 0
        self.assertIn('$0.00', render_providers(snapshot))
        del snapshot['rows'][0]['cost_usd']
        del snapshot['rows'][0]['cost_responses']
        self.assertIn('Total: 175 tokens · N/A USD', render_providers(snapshot))

    def test_share_denominator_includes_hidden_rows(self):
        snapshot = {'start': self.when, 'end': self.now.isoformat(), 'rows': [
            {'provider': 'p', 'model': f'm{i}', 'agent': 'pi', 'input_tokens': 10,
             'responses': 1, 'sessions': 1} for i in range(13)]}
        rendered = render_providers(snapshot)
        self.assertIn('Total: 130 tokens · N/A USD', rendered)
        self.assertIn('7.7%', rendered)
        self.assertIn('Showing 12 of 13', rendered)
        visible = rendered.split('```', 1)[0]
        self.assertNotIn('m12 ', visible)
        self.assertNotIn('m12 ', rendered)

    def test_compact_model_rows_stay_short_without_usage_details(self):
        model = 'model-' + 'x' * 40
        snapshot = {'start': self.when, 'end': self.now.isoformat(), 'rows': [
            {'provider': 'p', 'model': model, 'agent': 'pi', 'input_tokens': 2_000_000,
             'responses': 1, 'sessions': 1, 'cost_usd': 0.00001, 'cost_responses': 1}]}
        rendered = render_providers(snapshot)
        visible = rendered.split('```', 1)[0]
        self.assertIn('2.00M', visible)
        self.assertIn('<$0.01', visible)
        self.assertNotIn(model, visible)
        self.assertNotIn('<details>', rendered)
        self.assertNotIn('Sources & usage details', rendered)
        self.assertEqual(snapshot['rows'][0]['model'], model)
        self.assertTrue(all(len(line) <= 85 for line in visible.splitlines()))

    def test_safe_labels_reject_urls_credentials_and_markdown(self):
        for value in ["https://gateway/api?key=secret", "sk-secret", "waka_secret", "bad|row", None]:
            self.assertEqual(safe_label(value), "unknown")

    def test_rendering_preserves_other_sections_and_keeps_date_last(self):
        self.log(".pi/agent/sessions/a.jsonl", self.pi_rows())
        snapshot = collect(self.home, self.now)
        readme = ("<!--START_SECTION:ai-coding-->🤖 **AI Coding This Month**\n\n"
                  "```text\n⏱ AI Coding Time: not reported\n✍️ Lines changed: AI 10 · human 0\n"
                  "🔤 999 Input Tokens\n🧠 50 AI Sessions\nWakapi models · share of AI lines\n"
                  "old-model █████ 100%\n"
                  "── Local provider / model usage · token share ──\nTokens old total\n"
                  "old-local-model 100%\n```\n<!--END_SECTION:ai-coding-->\n\n"
                  "<!--START_SECTION:ai-providers-->old<!--END_SECTION:ai-providers-->\n\n"
                  " Last Updated on 13 November 2026 UTC\n\ncontact")
        result = normalize_layout(update_provider_section(readme, snapshot), "14 November 2026")
        self.assertIn("✍️ Lines changed: 10", result)
        self.assertNotIn('Local provider / model usage', result)
        self.assertIn("gpt-example", result)
        self.assertIn("Total: 175 tokens · $999.00 USD", result)
        visible = result.split('```text', 1)[1].split('```', 1)[0]
        self.assertIn("100.0%", visible)
        self.assertIn("█" * 15, visible)
        self.assertIn("gpt-example [pi]", visible)
        self.assertNotIn("not reported", visible)
        self.assertNotIn("old-model", visible)
        self.assertNotIn("old-local-model", visible)
        self.assertNotIn("old total", visible)
        self.assertNotIn("999 Input Tokens", visible)
        self.assertNotIn("50 AI Sessions", visible)
        self.assertNotIn("Cache Read", visible)
        self.assertNotIn("sessions;", visible)
        self.assertNotIn('<details>', result)
        self.assertNotIn('Sources & usage details', result)
        self.assertNotIn('see details below', result)
        self.assertNotIn("| Provider |", result)
        self.assertEqual(result.count("```"), 2)
        self.assertEqual(result.count("AI Coding This Month"), 1)
        self.assertEqual(result.count(LOCAL_USAGE_DIVIDER), 1)
        self.assertLess(result.index(LOCAL_USAGE_DIVIDER), result.index("<!--END_SECTION:ai-coding-->"))
        self.assertIn("<!--START_SECTION:ai-providers-->\n<!--END_SECTION:ai-providers-->", result)
        target = self.home / 'README.md'
        legacy = result.replace('<!--END_SECTION:ai-coding-->',
                                '<details><summary>Sources & usage details</summary>old</details>\n'
                                '<!--END_SECTION:ai-coding-->')
        self.assertEqual(update_provider_section(legacy, snapshot), result)
        target.write_text(result)
        update_readme(str(target), "🤖 **AI Coding This Month**\n\n```text\n✍️ Lines changed: AI 20 · human 1\n```\n")
        refreshed = target.read_text()
        self.assertIn('✍️ Lines changed: 21', refreshed)
        self.assertNotIn('AI 10 · human 0', refreshed)
        self.assertIn('Total: 175 tokens · $999.00 USD', refreshed)
        self.assertEqual(refreshed.count('```'), 2)
        self.assertEqual(refreshed.count(LOCAL_USAGE_DIVIDER), 1)
        self.assertEqual(update_provider_section(refreshed, snapshot), refreshed)
        self.assertGreater(result.index("Last Updated"), result.index("END_SECTION:ai-providers"))
        self.assertEqual(result.count("Last Updated"), 1)
        self.assertEqual(update_provider_section(result, snapshot), result)
        self.assertNotIn('Sources & usage details', refreshed)


if __name__ == "__main__":
    unittest.main()
