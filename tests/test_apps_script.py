import unittest
from unittest.mock import Mock, patch

import requests

from leetcode_table_bot.apps_script import ScriptClient, ScriptRepository
from leetcode_table_bot.config import BotError
from leetcode_table_bot.leetcode import Problem
from test_core import config


class ScriptTests(unittest.TestCase):
    def test_read_recovers_after_timeout_or_connection_failure(self):
        response = Mock()
        response.json.return_value = {"ok": True, "data": {"rows": []}}
        for action in ("metadata", "read"):
            with self.subTest(action=action):
                with patch("leetcode_table_bot.apps_script.requests.post", side_effect=[
                    requests.ReadTimeout(), requests.ConnectionError(), response,
                ]) as post, patch("leetcode_table_bot.apps_script.time.sleep") as sleep:
                    self.assertEqual(ScriptClient(config()).call(action), {"rows": []})
                self.assertEqual(post.call_count, 3)
                self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4])

    def test_read_timeout_is_bounded_and_explains_response_delivery(self):
        with patch("leetcode_table_bot.apps_script.requests.post", side_effect=requests.ReadTimeout()) as post, patch("leetcode_table_bot.apps_script.time.sleep"):
            with self.assertRaisesRegex(BotError, "после трёх попыток"):
                ScriptClient(config()).call("read")
        self.assertEqual(post.call_count, 3)

    def test_write_is_never_retried_after_an_ambiguous_failure(self):
        for action in ("append", "initialize"):
            for error in (requests.ReadTimeout(), requests.ConnectionError()):
                with self.subTest(action=action, error=type(error).__name__):
                    with patch("leetcode_table_bot.apps_script.requests.post", side_effect=error) as post, patch("leetcode_table_bot.apps_script.time.sleep") as sleep:
                        with self.assertRaises(BotError):
                            ScriptClient(config()).call(action)
                    post.assert_called_once()
                    sleep.assert_not_called()

    def test_certificate_failure_is_not_retried(self):
        with patch("leetcode_table_bot.apps_script.requests.post", side_effect=requests.exceptions.SSLError()) as post, patch("leetcode_table_bot.apps_script.time.sleep") as sleep:
            with self.assertRaisesRegex(BotError, "сертификат"):
                ScriptClient(config()).call("read")
        post.assert_called_once()
        sleep.assert_not_called()

    def test_transient_http_failure_retried_only_for_reads(self):
        for status in (502, 503, 504):
            for action in ("read", "append"):
                with self.subTest(status=status, action=action):
                    unavailable = requests.Response()
                    unavailable.status_code = status
                    response = Mock()
                    response.json.return_value = {"ok": True, "data": {}}
                    with patch("leetcode_table_bot.apps_script.requests.post", side_effect=[
                        requests.HTTPError(response=unavailable), response,
                    ]) as post, patch("leetcode_table_bot.apps_script.time.sleep"):
                        if action == "read":
                            self.assertEqual(ScriptClient(config()).call(action), {})
                            self.assertEqual(post.call_count, 2)
                        else:
                            with self.assertRaises(BotError):
                                ScriptClient(config()).call(action)
                            post.assert_called_once()

    def test_token_only_sent_to_google_in_post_body(self):
        response = Mock()
        response.json.return_value = {"ok": True, "data": {}}
        with patch("leetcode_table_bot.apps_script.requests.post", return_value=response) as post:
            ScriptClient(config()).call("read")
        self.assertEqual(post.call_args.args[0], config().script_url)
        self.assertEqual(post.call_args.kwargs["json"]["token"], config().script_token)

    def test_401_has_deployment_instruction(self):
        response = requests.Response()
        response.status_code = 401
        with patch("leetcode_table_bot.apps_script.requests.post", side_effect=requests.HTTPError(response=response)) as post:
            with self.assertRaisesRegex(BotError, "Anyone"):
                ScriptClient(config()).call("read")
        post.assert_called_once()

    def test_server_error_mentions_current_setup_flag(self):
        response = Mock()
        response.json.return_value = {"ok": False, "error": "Введите токен через --apps-script"}
        with patch("leetcode_table_bot.apps_script.requests.post", return_value=response):
            with self.assertRaisesRegex(BotError, "--setup"):
                ScriptClient(config()).call("read")

    def test_append_uses_existing_deployed_script_contract(self):
        with patch.object(ScriptClient, "call", return_value={"title": "Лист1", "col_count": 26, "supports_resolve": True}):
            repository = ScriptRepository(config())
        with patch.object(repository.client, "call", return_value={}) as call:
            repository.append(Problem(228, "Summary Ranges", "summary-ranges"), "=буквальный текст", "ДА")
        call.assert_called_once_with("append", number=228, title="Summary Ranges", notes="=буквальный текст", url="https://leetcode.com/problems/summary-ranges/", resolve="ДА")

    def test_old_google_deployment_is_rejected_before_notes_are_requested(self):
        with patch.object(ScriptClient, "call", return_value={"title": "Лист1", "col_count": 26}):
            with self.assertRaisesRegex(BotError, "старый Apps Script"):
                ScriptRepository(config())
