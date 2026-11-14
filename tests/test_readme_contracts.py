"""Ownership, preservation, and repeatability contracts; not presentation snapshots."""
import copy
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from export_ai_providers import update_provider_section
from wakapi_ai_stats import (
    COMMIT_PREFIX, END_MARKER, LOCAL_USAGE_DIVIDER, START_MARKER,
    aggregate, normalize_layout, render, render_coding_month, render_providers,
    replace_coding_month, replace_section, update_readme,
)


class ReadmeContracts(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 11, 1, tzinfo=timezone.utc)
        self.end = datetime(2026, 11, 14, tzinfo=timezone.utc)
        self.snapshot = {'version': 2, 'source': 'local-session-logs',
                         'start': self.start.isoformat(), 'end': self.end.isoformat(),
                         'generated_at': self.end.isoformat(), 'github_commits': 114,
                         'rows': [{'agent': 'pi', 'provider': 'provider', 'model': 'model-v1',
                                   'input_tokens': 100, 'output_tokens': 20,
                                   'cache_read_tokens': 50, 'cache_write_tokens': 5,
                                   'sessions': 1, 'responses': 1, 'cost_usd': 0.1,
                                   'cost_responses': 1}]}
        self.repo = '💻 **I Mostly Code in TypeScript**\n\n```text\nrepository-language-data\n```'
        self.waka = ('<!--START_SECTION:waka-->\n📊 **This Week I Spent My Time On**\n\n'
                     '```text\nold-duration-data\n```\n\n' + self.repo + '\n<!--END_SECTION:waka-->')
        self.summary = aggregate({}, [{'time': self.start.timestamp(), 'ai_line_changes': 10}],
                                 self.start, self.end)
        self.original = ('unowned-prefix\n\n' + self.waka + '\n\n' + START_MARKER + '\n'
                         + render(self.summary) + END_MARKER + '\n\n'
                         '<!--START_SECTION:ai-providers-->legacy<!--END_SECTION:ai-providers-->\n\n'
                         'unowned-suffix\n')

    def section(self, readme, name):
        return readme.split(f'<!--START_SECTION:{name}-->', 1)[1].split(f'<!--END_SECTION:{name}-->', 1)[0]

    def test_ai_replacement_owns_only_its_markers(self):
        prefix, remainder = self.original.split(START_MARKER)
        _, suffix = remainder.split(END_MARKER)
        updated = replace_section(self.original, 'generated-content')
        self.assertTrue(updated.startswith(prefix + START_MARKER))
        self.assertTrue(updated.endswith(END_MARKER + suffix))
        self.assertEqual(self.section(updated, 'ai-coding').strip(), 'generated-content')

    def test_invalid_marker_ownership_is_rejected(self):
        for invalid in ['', END_MARKER + START_MARKER, START_MARKER * 2 + END_MARKER]:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    replace_section(invalid, 'generated')
        for invalid in ['', self.original.replace('<!--END_SECTION:ai-providers-->', ''),
                        self.original + '<!--START_SECTION:ai-providers-->']:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    update_provider_section(invalid, self.snapshot)
        with self.assertRaises(ValueError):
            replace_coding_month('<!--END_SECTION:waka--><!--START_SECTION:waka-->', 'generated')

    def test_local_refresh_is_idempotent_and_does_not_mutate_inputs_or_other_sources(self):
        snapshot = copy.deepcopy(self.snapshot)
        updated = update_provider_section(self.original, snapshot)
        self.assertEqual(snapshot, self.snapshot)
        self.assertIn(self.waka, updated)
        self.assertTrue(updated.startswith('unowned-prefix\n\n'))
        self.assertTrue(updated.endswith('unowned-suffix\n'))
        self.assertEqual(update_provider_section(updated, snapshot), updated)
        self.assertEqual(self.section(updated, 'ai-coding').count('```'), 2)
        for name in ['waka', 'ai-coding', 'ai-providers']:
            self.assertEqual(updated.count(f'<!--START_SECTION:{name}-->'), 1)
            self.assertEqual(updated.count(f'<!--END_SECTION:{name}-->'), 1)

    def test_wakapi_refresh_preserves_local_usage_and_github_count(self):
        local = update_provider_section(self.original, self.snapshot)
        previous = self.section(local, 'ai-coding')
        retained_usage = previous.split(LOCAL_USAGE_DIVIDER, 1)[1]
        retained_commit = next(line for line in previous.splitlines() if line.startswith(COMMIT_PREFIX))
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'README.md'
            path.write_text(local)
            stats = {'timezone': 'UTC', 'languages': [{'name': 'Python', 'total_seconds': 7200, 'percent': 100}]}
            summary = aggregate(stats, [{'time': self.start.timestamp(), 'ai_line_changes': 30}],
                                self.start, self.end)
            update_readme(str(path), render(summary), render_coding_month(stats))
            refreshed = path.read_text()
        actual = self.section(refreshed, 'ai-coding')
        self.assertEqual(actual.split(LOCAL_USAGE_DIVIDER, 1)[1], retained_usage)
        self.assertEqual([line for line in actual.splitlines() if line.startswith(COMMIT_PREFIX)], [retained_commit])
        self.assertIn(self.repo, refreshed)
        self.assertEqual(update_provider_section(refreshed, self.snapshot), refreshed)
        self.assertTrue(refreshed.startswith('unowned-prefix\n\n'))
        self.assertTrue(refreshed.endswith('unowned-suffix\n'))

    def test_coding_replacement_preserves_repository_stats_and_unowned_content(self):
        stats = {'timezone': 'UTC', 'languages': [{'name': 'Python', 'total_seconds': 3600, 'percent': 100}]}
        before = copy.deepcopy(stats)
        coding = render_coding_month(stats)
        updated = replace_coding_month(self.original, coding)
        prefix = self.original.split('<!--START_SECTION:waka-->')[0]
        suffix = self.original.split('<!--END_SECTION:waka-->')[1]
        self.assertTrue(updated.startswith(prefix))
        self.assertTrue(updated.endswith(suffix))
        self.assertIn(self.repo, updated)
        self.assertEqual(replace_coding_month(updated, coding), updated)
        self.assertEqual(stats, before)
        self.assertEqual(coding.count('```'), 2)

    def test_monthly_coding_rejects_invalid_api_values(self):
        for stats in [{'languages': {}}, {'editors': [{'total_seconds': float('inf')}]},
                      {'operating_systems': [{'percent': float('nan')}]}]:
            with self.subTest(stats=stats):
                with self.assertRaises(ValueError):
                    render_coding_month(stats)

    def test_month_migration_does_not_relabel_weekly_telemetry(self):
        weekly = ('unowned-prefix\n' + START_MARKER + '\n🤖 **AI Coding This Week**\n\n'
                  '```text\nLines changed: 999999\n```\n' + END_MARKER + '\n'
                  '<!--START_SECTION:ai-providers-->old<!--END_SECTION:ai-providers-->\nunowned-suffix')
        updated = update_provider_section(weekly, self.snapshot)
        # Sentinel old telemetry must be discarded, not presented as a monthly measurement.
        self.assertNotIn('999999', self.section(updated, 'ai-coding'))
        self.assertEqual(update_provider_section(updated, self.snapshot), updated)

    def test_layout_normalization_is_idempotent_and_retains_data_regions(self):
        updated = normalize_layout(update_provider_section(self.original, self.snapshot), '14 November 2026')
        self.assertEqual(normalize_layout(updated, '14 November 2026'), updated)
        self.assertEqual(self.section(updated, 'waka'), self.section(self.original, 'waka'))
        self.assertIn(self.repo, updated)
        for name in ['waka', 'ai-coding', 'ai-providers']:
            self.assertEqual(updated.count(f'<!--START_SECTION:{name}-->'), 1)
            self.assertEqual(updated.count(f'<!--END_SECTION:{name}-->'), 1)

    def test_absent_commit_data_is_not_fabricated_and_zero_is_valid(self):
        snapshot = copy.deepcopy(self.snapshot)
        for value in [None, True, -1, '114']:
            with self.subTest(value=value):
                snapshot['github_commits'] = value
                self.assertFalse(any(line.startswith(COMMIT_PREFIX) for line in render_providers(snapshot).splitlines()))
        snapshot['github_commits'] = 0
        line = next(line for line in render_providers(snapshot).splitlines() if line.startswith(COMMIT_PREFIX))
        self.assertEqual(int(line.removeprefix(COMMIT_PREFIX)), 0)


if __name__ == '__main__':
    unittest.main()
