"""Offline regression tests: no browser, account, or API requests."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

import gemini_client as gemini
import snap_bot as bot


class OfflineTestCase(unittest.TestCase):
    def use_patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result


class CycleTests(OfflineTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        values = dict(FRIENDS_FILE=root / 'friends.json', RUNTIME_FILE=root / 'runtime.json',
                      LOG_FILE=root / 'conversations.log', WHITELIST=['demo contact'], BLACKLIST=[],
                      WAVE_SIZE=0, MAX_PER_DAY=40, MAX_REPLIES=3, TELEGRAM_LINK='https://t.me/example')
        self.patches = patch.multiple(bot, **values)
        self.patches.start()
        self.addCleanup(self.patches.stop)
        self.page = MagicMock()
        self.page.locator.return_value.count.return_value = 1
        item = self.page.locator.return_value.nth.return_value
        item.inner_text.return_value = 'Demo Contact\nNew chat'
        item.locator.return_value.count.return_value = 1
        self.use_patch(patch.object(bot, 'read_conversation_text', return_value='come va?'))
        self.generate = self.use_patch(patch.object(bot, 'generate_reply', return_value='https://t.me/example'))
        self.send = self.use_patch(patch.object(bot, 'send_message', return_value=True))
        self.use_patch(patch.object(bot.time, 'sleep'))
        self.rt = {'date': bot.time.strftime('%Y-%m-%d'), 'sent': 2}

    def test_preview_does_not_mutate_existing_state_or_persist(self):
        friends = {}
        bot.get_friend(friends, 'Demo Contact')
        original = copy.deepcopy(friends)
        runtime = dict(self.rt)
        previewed = {}
        self.assertEqual(bot.process_cycle(self.page, True, friends, self.rt, previewed), 1)
        self.assertEqual(friends, original)
        self.assertEqual(self.rt, runtime)
        self.assertFalse(bot.FRIENDS_FILE.exists())
        self.assertFalse(bot.RUNTIME_FILE.exists())
        self.send.assert_not_called()
        self.assertEqual(bot.process_cycle(self.page, True, friends, self.rt, previewed), 0)
        self.assertEqual(self.generate.call_count, 1)

    def test_preview_does_not_create_friend_or_block_later_real_send(self):
        friends = {}
        bot.process_cycle(self.page, True, friends, self.rt)
        self.assertEqual(friends, {})
        self.assertEqual(bot.process_cycle(self.page, False, friends, self.rt), 1)
        rec = bot.load_friends()['Demo Contact']
        self.assertEqual(rec['pitched_count'], 1)
        self.assertEqual(rec['stage'], 'invitato')
        self.assertEqual(bot.load_runtime()['sent'], 3)
        self.assertEqual(bot.process_cycle(self.page, False, friends, self.rt), 0)
        self.send.assert_called_once()

    def test_failed_send_does_not_change_state(self):
        self.send.return_value = False
        friends = {}
        self.assertEqual(bot.process_cycle(self.page, False, friends, self.rt), 0)
        self.assertEqual(friends, {})
        self.assertEqual(self.rt['sent'], 2)
        self.assertFalse(bot.FRIENDS_FILE.exists())

    def test_daily_limit_prevents_generation(self):
        self.rt['sent'] = 40
        self.assertEqual(bot.process_cycle(self.page, False, {}, self.rt), 0)
        self.generate.assert_not_called()

    def test_corrupt_state_is_not_silently_reset(self):
        bot.FRIENDS_FILE.write_text('{')
        bot.RUNTIME_FILE.write_text('{')
        for loader in (bot.load_friends, bot.load_runtime):
            with self.assertRaises(ValueError):
                loader()
        self.assertEqual(bot.FRIENDS_FILE.read_text(), '{')

    def test_invalid_records_rejected(self):
        bot.FRIENDS_FILE.write_text(json.dumps({'Demo Contact': {'history': []}}))
        bot.RUNTIME_FILE.write_text(json.dumps({'date': self.rt['date'], 'sent': -1}))
        for loader in (bot.load_friends, bot.load_runtime):
            with self.assertRaises(ValueError):
                loader()

    def test_new_day_resets_counter(self):
        bot.save_runtime({'date': '2000-01-01', 'sent': 40})
        self.assertEqual(bot.load_runtime(), {'date': self.rt['date'], 'sent': 0})

    def test_whitelist_matches_exactly_and_blacklist_wins(self):
        self.assertTrue(bot.allowed('DEMO CONTACT', ['demo contact']))
        self.assertFalse(bot.allowed('Demo Contact other', ['demo contact']))
        with patch.object(bot, 'BLACKLIST', ['demo contact']):
            self.assertFalse(bot.allowed('Demo Contact', ['demo contact']))
        with patch.object(bot, 'WHITELIST', []):
            self.assertFalse(bot.allowed('Demo Contact', []))
            with self.assertRaises(ValueError):
                bot.validate_config(send=True)

    def test_invalid_delays_and_wave_interval_rejected(self):
        for settings in ({'WAVE_MINUTES': 0}, {'DELAY_MIN': -1},
                         {'DELAY_MAX': float('nan')}, {'ACTIVE_START': 24}):
            with self.subTest(settings=settings), patch.multiple(bot, **settings):
                with self.assertRaises(ValueError):
                    bot.validate_config()

    def test_overnight_active_hours(self):
        with patch.multiple(bot, ACTIVE_START=22, ACTIVE_END=6):
            for hour, active in [('23', True), ('05', True), ('06', False), ('12', False)]:
                with patch.object(bot.time, 'strftime', return_value=hour):
                    self.assertEqual(bot.within_active_hours(), active)

    def test_inspection_writes_aria_yaml(self):
        self.page.locator.return_value.aria_snapshot.return_value = '- text: hello'
        with patch.object(bot, 'BASE', Path(self.temp.name)), patch('builtins.input'):
            bot.run_inspect(self.page)
        self.assertEqual((Path(self.temp.name) / 'inspect_dump.yaml').read_text(), '- text: hello')


class GeminiTests(OfflineTestCase):
    def setUp(self):
        self.use_patch(patch.multiple(gemini, API_KEY='test-secret', MODEL='test-model'))
        self.post = self.use_patch(patch.object(gemini.requests, 'post'))
        self.response = self.post.return_value
        self.response.status_code = 200
        self.response.json.return_value = {'candidates': [{'finishReason': 'STOP', 'content': {
            'parts': [{'text': 'private thought', 'thought': True}, {'text': '"ciao"'}]}}]}

    def generate(self):
        return gemini.generate_reply('persona', 'funnel', 'hello', 'Demo Contact', 'nuovo', False, '')

    def test_reply_excludes_thoughts_and_key_is_not_in_url(self):
        self.assertEqual(self.generate(), 'ciao')
        args, kwargs = self.post.call_args
        self.assertNotIn('test-secret', args[0])
        self.assertNotIn('params', kwargs)
        self.assertEqual(kwargs['headers']['x-goog-api-key'], 'test-secret')

    def test_network_errors_do_not_expose_credentials(self):
        self.post.side_effect = requests.RequestException('secret=test-secret')
        with self.assertRaises(gemini.GeminiError) as caught:
            self.generate()
        self.assertNotIn('test-secret', str(caught.exception))

    def test_http_errors_do_not_echo_response_body(self):
        self.response.status_code = 429
        self.response.text = 'test-secret'
        with self.assertRaises(gemini.GeminiError) as caught:
            self.generate()
        self.assertIn('429', str(caught.exception))
        self.assertNotIn('test-secret', str(caught.exception))

    def test_invalid_json_is_reported(self):
        self.response.json.side_effect = ValueError('invalid')
        with self.assertRaises(gemini.GeminiError):
            self.generate()

    def test_blocked_truncated_and_empty_replies_are_rejected(self):
        for data in ({}, {'candidates': [{'finishReason': 'MAX_TOKENS'}]},
                     {'candidates': [{'finishReason': 'STOP', 'content': {'parts': [{'text': '""'}]}}]}):
            with self.subTest(data=data):
                self.response.json.return_value = data
                with self.assertRaises(gemini.GeminiError):
                    self.generate()

    def test_missing_model_fails_before_request(self):
        with patch.object(gemini, 'MODEL', ''):
            with self.assertRaises(gemini.GeminiError):
                self.generate()
        self.post.assert_not_called()


if __name__ == '__main__':
    unittest.main()
