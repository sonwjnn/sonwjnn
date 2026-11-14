"""Aggregate and external-adapter contracts: see CONTRACTS.md."""
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from subprocess import CompletedProcess, TimeoutExpired
from tempfile import TemporaryDirectory
from unittest.mock import patch

from export_ai_providers import (
    collect, fetch_month_commits, main, recorded_cost, safe_label,
)


class ExportContracts(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.now = datetime(2026, 11, 14, 10, tzinfo=timezone.utc)

    def log(self, path, rows):
        target = self.home / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('\n'.join(json.dumps(row) for row in rows))

    def pi_event(self, identity='response', when='2026-11-02T12:00:00Z', cost=0.1):
        return {'type': 'message', 'id': identity, 'timestamp': when,
                'message': {'role': 'assistant', 'responseId': identity,
                            'provider': 'provider', 'model': 'model-v1',
                            'content': 'PRIVATE_PROMPT sk-private-key',
                            'usage': {'input': 100, 'output': 20, 'cacheRead': 50,
                                      'cacheWrite': 5, 'cost': {'total': cost}}}}

    def test_allowlisted_snapshot_deduplicates_forks_and_excludes_private_data(self):
        rows = [{'type': 'session', 'id': 'PRIVATE_SESSION', 'cwd': 'PRIVATE_PATH'}, self.pi_event()]
        self.log('.pi/agent/sessions/a.jsonl', rows)
        self.log('.pi/agent/sessions/fork.jsonl', rows)
        snapshot = collect(self.home, self.now)
        self.assertEqual(set(snapshot), {'version', 'source', 'generated_at', 'start', 'end', 'rows'})
        self.assertEqual(snapshot['rows'], [{
            'agent': 'pi', 'provider': 'provider', 'model': 'model-v1',
            'input_tokens': 100, 'output_tokens': 20, 'cache_read_tokens': 50,
            'cache_write_tokens': 5, 'responses': 1, 'sessions': 1,
            'cost_usd': 0.1, 'cost_responses': 1,
        }])
        serialized = json.dumps(snapshot)
        for private in ['PRIVATE_SESSION', 'PRIVATE_PATH', 'PRIVATE_PROMPT', 'sk-private-key', str(self.home)]:
            with self.subTest(private=private):
                self.assertNotIn(private, serialized)

    def test_calendar_month_is_half_open_and_not_a_trailing_week(self):
        times = ['2026-10-31T23:59:59Z', '2026-11-01T00:00:00Z',
                 '2026-11-02T12:00:00Z', self.now.isoformat(), '2026-12-01T00:00:00Z']
        self.log('.pi/agent/sessions/month.jsonl', [self.pi_event(str(i), when) for i, when in enumerate(times)])
        snapshot = collect(self.home, self.now)
        self.assertEqual(snapshot['start'], '2026-11-01T00:00:00+00:00')
        self.assertEqual(snapshot['end'], self.now.isoformat())
        self.assertEqual(snapshot['rows'][0]['responses'], 2)
        self.assertEqual(snapshot['rows'][0]['input_tokens'], 200)

    def test_month_window_normalizes_now_to_utc(self):
        now = datetime(2026, 3, 1, 1, tzinfo=timezone(timedelta(hours=7)))
        snapshot = collect(self.home, now)
        self.assertEqual(snapshot['start'], '2026-02-01T00:00:00+00:00')
        self.assertEqual(snapshot['end'], '2026-02-28T18:00:00+00:00')

    def test_missing_timestamp_and_empty_usage_are_excluded(self):
        missing = self.pi_event('missing', when=None)
        empty = self.pi_event('empty')
        empty['message']['usage'] = {}
        self.log('.pi/agent/sessions/a.jsonl', [missing, empty])
        self.assertEqual(collect(self.home, self.now)['rows'], [])

    def test_codex_uses_deltas_and_does_not_double_count_cache(self):
        self.log('.codex/sessions/a.jsonl', [
            {'type': 'session_meta', 'payload': {'id': 'private', 'model_provider': 'gateway'}},
            {'type': 'turn_context', 'payload': {'turn_id': 'turn', 'model': 'model-v2'}},
            *[{'type': 'token_usage_record', 'timestamp': self.now.timestamp() - 1,
               'payload': {'response_id': str(i), 'turn_id': 'turn', 'usage': usage,
                           'thread_token_usage': {'input_tokens': 999999}}}
              for i, usage in enumerate([
                  {'input_tokens': 100, 'cached_input_tokens': 40, 'cache_write_input_tokens': 5, 'output_tokens': 10},
                  {'input_tokens': 200, 'cached_input_tokens': 50, 'output_tokens': 20}])],
        ])
        row = collect(self.home, self.now)['rows'][0]
        self.assertEqual((row['provider'], row['model']), ('gateway', 'model-v2'))
        self.assertEqual((row['input_tokens'], row['cache_read_tokens'], row['cache_write_tokens'],
                          row['output_tokens'], row['responses'], row['sessions']), (205, 90, 5, 30, 2, 1))
        self.assertIsNone(row['cost_usd'])
        self.assertEqual(row['cost_responses'], 0)

    def test_claude_does_not_infer_provider_or_accept_synthetic_models(self):
        self.log('.claude/projects/a.jsonl', [
            {'type': 'assistant', 'timestamp': self.now.timestamp() - 1, 'sessionId': 'private',
             'message': {'id': 'r1', 'model': 'claude-version', 'usage': {'input_tokens': 10}}},
            {'type': 'assistant', 'timestamp': self.now.timestamp() - 1,
             'message': {'model': '<synthetic>', 'usage': {'input_tokens': 999}}},
        ])
        rows = collect(self.home, self.now)['rows']
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['provider'], rows[0]['model'], rows[0]['input_tokens']),
                         ('unknown', 'claude-version', 10))
        self.assertIsNone(rows[0]['cost_usd'])

    def test_recorded_cost_validation_distinguishes_missing_and_zero(self):
        for invalid in [None, True, False, -1, float('nan'), float('inf'), '1.20', {}]:
            with self.subTest(invalid=invalid):
                self.assertIsNone(recorded_cost({'cost': {'total': invalid}}))
        self.assertIsNone(recorded_cost({'cost': 12}))
        self.assertEqual(recorded_cost({'cost': {'total': 0}}), 0)
        self.assertEqual(recorded_cost({'cost': {'total': 0.12}}), 0.12)

    def test_cost_and_coverage_follow_deduplicated_responses(self):
        missing = self.pi_event('missing')
        del missing['message']['usage']['cost']
        second = self.pi_event('second', cost=0.2)
        self.log('.pi/agent/sessions/a.jsonl', [self.pi_event(), second, second, missing])
        row = collect(self.home, self.now)['rows'][0]
        self.assertEqual((row['cost_usd'], row['cost_responses'], row['responses']), (0.3, 2, 3))
        self.log('.pi/agent/sessions/a.jsonl', [missing])
        row = collect(self.home, self.now)['rows'][0]
        self.assertIsNone(row['cost_usd'])
        self.assertEqual(row['cost_responses'], 0)

    def test_untrusted_labels_cannot_export_urls_credentials_or_markup(self):
        for value in ['https://gateway/?key=private', 'sk-private', 'waka_private', 'bad|row', None]:
            with self.subTest(value=value):
                self.assertEqual(safe_label(value), 'unknown')

    def test_github_adapter_sends_exact_range_and_accepts_only_integer_counts(self):
        start = self.now.replace(day=1, hour=0)
        for count in [0, 114]:
            with self.subTest(count=count), patch('export_ai_providers.subprocess.run') as run:
                run.return_value = CompletedProcess([], 0, str(count), '')
                self.assertEqual(fetch_month_commits(start, self.now), count)
                command = run.call_args.args[0]
                self.assertEqual(command[:3], ['gh', 'api', 'graphql'])
                self.assertIn('from=' + start.isoformat(), command)
                self.assertIn('to=' + self.now.isoformat(), command)
                self.assertIn('totalCommitContributions', command[4])
        for value in ['null', 'true', '-1', '1.5', '"114"', 'not json']:
            with self.subTest(value=value), patch('export_ai_providers.subprocess.run',
                                                 return_value=CompletedProcess([], 0, value, '')):
                with self.assertRaises(RuntimeError):
                    fetch_month_commits(start, self.now)

    def test_github_failures_are_sanitized_and_export_does_not_write_outputs(self):
        for error in [OSError('PRIVATE_ERROR'), TimeoutExpired('gh', 60)]:
            with self.subTest(error=error), patch('export_ai_providers.subprocess.run', side_effect=error):
                with self.assertRaises(RuntimeError) as raised:
                    fetch_month_commits(self.now, self.now)
                self.assertNotIn('PRIVATE_ERROR', str(raised.exception))
        output, readme = self.home / 'aggregate.json', self.home / 'README.md'
        readme.write_text('untouched')
        with patch('sys.argv', ['export', '--output', str(output), '--readme', str(readme)]), \
                patch('export_ai_providers.collect', return_value=collect(self.home, self.now)), \
                patch('export_ai_providers.subprocess.run', return_value=CompletedProcess([], 1, 'PRIVATE', 'PRIVATE')):
            with self.assertRaises(RuntimeError) as raised:
                main()
        self.assertNotIn('PRIVATE', str(raised.exception))
        self.assertFalse(output.exists())
        self.assertEqual(readme.read_text(), 'untouched')


if __name__ == '__main__':
    unittest.main()
