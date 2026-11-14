"""Wakapi boundary and aggregation contracts: see CONTRACTS.md."""
import base64
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from wakapi_ai_stats import _api_base, _fetch_month, _get_json, aggregate, reported_model


class WakapiContracts(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 11, 1, tzinfo=timezone.utc)
        self.end = datetime(2026, 11, 14, tzinfo=timezone.utc)

    def test_api_base_normalization_rejects_credentials_and_unexpected_targets(self):
        expected = 'https://wakapi.dev/api/compat/wakatime/v1'
        for url in ['https://wakapi.dev/api/', expected + '/']:
            with self.subTest(url=url):
                self.assertEqual(_api_base(url), expected)
        for url in ['file:///api', 'https://user:password@wakapi.dev/api',
                    'https://wakapi.dev/api?key=private', 'https://wakapi.dev/api#private',
                    'https://wakapi.dev/unrelated']:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    _api_base(url)

    def test_authentication_is_in_header_not_url(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return b'{"data": {}}'

        with patch('wakapi_ai_stats.urlopen', return_value=Response()) as request:
            self.assertEqual(_get_json('https://wakapi.dev/test', 'private-key'), {'data': {}})
        sent = request.call_args.args[0]
        self.assertNotIn('private-key', sent.full_url)
        self.assertEqual(base64.b64decode(sent.get_header('Authorization').split(' ', 1)[1]), b'private-key')

    def test_month_adapter_uses_returned_timezone_and_half_open_range(self):
        stats = {'start': '2026-11-01T00:00:00+07:00',
                 'end': '2026-11-14T00:00:00+07:00', 'timezone': 'Asia/Saigon'}
        start, end = datetime.fromisoformat(stats['start']), datetime.fromisoformat(stats['end'])
        entries = [{'time': value} for value in [start.timestamp() - 1, start.timestamp(),
                                               start.timestamp() + 86400, end.timestamp()]]
        calls = []

        def response(url, _key):
            calls.append(url)
            if url.endswith('/stats/month'):
                return {'data': stats}
            return {'data': entries if 'date=2026-11-01' in url else []}

        with patch('wakapi_ai_stats._get_json', side_effect=response):
            returned_stats, heartbeats, actual_start, actual_end = _fetch_month('https://wakapi.dev/api', 'key')
        self.assertEqual(returned_stats, stats)
        self.assertEqual((actual_start, actual_end), (start, end))
        self.assertEqual([event['time'] for event in heartbeats], [start.timestamp(), start.timestamp() + 86400])
        self.assertEqual(calls[0], 'https://wakapi.dev/api/users/current/stats/month')
        self.assertEqual(len(calls), 15)
        self.assertTrue(calls[1].endswith('date=2026-11-01'))
        self.assertTrue(calls[-1].endswith('date=2026-11-14'))

    def test_month_adapter_rejects_invalid_remote_shapes(self):
        for response in [{}, {'data': []}]:
            with self.subTest(response=response), patch('wakapi_ai_stats._get_json', return_value=response):
                with self.assertRaises(RuntimeError):
                    _fetch_month('https://wakapi.dev/api', 'key')
        stats = {'start': self.start.isoformat(), 'end': self.end.isoformat()}
        with patch('wakapi_ai_stats._get_json', side_effect=[{'data': stats}, {'data': {}}]):
            with self.assertRaises(RuntimeError):
                _fetch_month('https://wakapi.dev/api', 'key')

    def test_aggregation_preserves_counts_and_excludes_out_of_range_events(self):
        stats = {'categories': [{'text': 'AI Coding', 'total_seconds': 7200, 'percent': 25}]}
        heartbeats = [
            {'time': self.start.timestamp(), 'ai_model': 'model-v1', 'ai_session': 'session',
             'ai_line_changes': 10, 'human_line_changes': 2, 'ai_input_tokens': 1000,
             'ai_output_tokens': 400, 'ai_prompt_length': 50},
            {'time': self.start.timestamp() + 86400, 'ai_model': 'model-v1', 'ai_session': 'session',
             'ai_line_changes': 5, 'human_line_changes': 0, 'ai_input_tokens': 500,
             'ai_output_tokens': 200, 'ai_prompt_length': 30},
            {'time': self.start.timestamp() + 1, 'human_line_changes': 3},
            {'time': self.start.timestamp() - 1, 'ai_line_changes': 999},
            {'time': self.end.timestamp(), 'ai_line_changes': 999},
        ]
        self.assertEqual(aggregate(stats, heartbeats, self.start, self.end), {
            'ai_seconds': 7200, 'ai_percent': 25, 'ai_lines': 15, 'human_lines': 5,
            'input_tokens': 1500, 'output_tokens': 600, 'sessions': 1,
            'sessions_reported': True, 'prompt_events': 2, 'prompt_events_reported': True,
            'ai_event_count': 2, 'models': {'model-v1': {
                'ai_lines': 15, 'input_tokens': 1500, 'output_tokens': 600, 'events': 2}},
        })

    def test_missing_telemetry_stays_unknown(self):
        result = aggregate({'categories': []}, [], self.start, self.end)
        self.assertIsNone(result['ai_seconds'])
        self.assertIsNone(result['ai_percent'])
        self.assertFalse(result['sessions_reported'])
        self.assertFalse(result['prompt_events_reported'])
        self.assertEqual(result['models'], {})

    def test_model_identity_uses_only_matching_reported_version(self):
        cases = [
            ({'ai_model': 'Sonnet', 'user_agent_id': 'wakatime/1 sonnet/4.5 claude-code/2'}, 'sonnet/4.5'),
            ({'ai_model': 'Gpt', 'user_agent_id': 'wakatime/1 codex/5.2'}, 'Gpt'),
            ({'ai_model': 'Gpt', 'user_agent_id': 'gpt/unknown'}, 'Gpt'),
            ({'user_agent_id': 'gpt/5.2'}, ''),
            ({'ai_model': 'openrouter/openai/model-v1'}, 'openrouter/openai/model-v1'),
        ]
        for event, expected in cases:
            with self.subTest(event=event):
                self.assertEqual(reported_model(event), expected)
        events = [{'time': self.start.timestamp(), 'ai_model': 'Sonnet', 'ai_input_tokens': 10,
                   'user_agent_id': f'sonnet/{version} runtime/9'} for version in ['4.5', '4.6']]
        summary = aggregate({}, events, self.start, self.end)
        self.assertEqual(set(summary['models']), {'sonnet/4.5', 'sonnet/4.6'})
        self.assertEqual(summary['input_tokens'], 20)


if __name__ == '__main__':
    unittest.main()
