from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from rich.console import Console

from leetcode_table_bot.apps_script import ScriptClient, ScriptRepository
from leetcode_table_bot.cli import first_setup
from leetcode_table_bot.config import BotError, Config, DEFAULT_COLUMNS, validate_script_url
from leetcode_table_bot.leetcode import Problem
from leetcode_table_bot.sheets import ExistingEntry, connect
from leetcode_table_bot.ui import TerminalUI

URL = "https://docs.google.com/spreadsheets/d/example/edit#gid=0"
SCRIPT_URL = "https://script.google.com/macros/s/example-deployment/exec"
TOKEN = "a" * 64


def script_config():
    return Config(URL, columns=dict(DEFAULT_COLUMNS), backend="apps_script", script_url=SCRIPT_URL, script_token=TOKEN)


class ScriptConfigTests(unittest.TestCase):
    def test_only_google_exec_url_is_allowed(self):
        self.assertEqual(validate_script_url(SCRIPT_URL), SCRIPT_URL)
        for value in [SCRIPT_URL.replace("/exec", "/dev"), SCRIPT_URL.replace("https:", "http:"),
                      SCRIPT_URL.replace("script.google.com", "script.google.com.evil.test"),
                      SCRIPT_URL + "?token=secret", SCRIPT_URL + "#fragment"]:
            with self.subTest(value=value), self.assertRaises(BotError):
                validate_script_url(value)

    def test_token_not_in_config_repr(self):
        self.assertNotIn(TOKEN, repr(script_config()))

    def test_deployment_id_is_rejected_as_token(self):
        for token in ["AKfy" + "a" * 60, "AKfy" + "a" * 144, SCRIPT_URL]:
            with self.subTest(length=len(token)), self.assertRaisesRegex(BotError, "ID развёртывания"):
                Config(URL, backend="apps_script", script_url=SCRIPT_URL, script_token=token)

    def test_old_config_still_uses_google_api(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"spreadsheet_url": URL}))
            self.assertEqual(Config.load(path).backend, "google")

    def test_switch_to_script_keeps_sheet_and_does_not_ask_for_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            Config(URL, columns=dict(DEFAULT_COLUMNS)).save(path)
            output = io.StringIO()
            ui = TerminalUI()
            ui.console = Console(file=output, no_color=True)
            with patch.object(ui.console, "input", side_effect=[URL, SCRIPT_URL, TOKEN]):
                config = first_setup(ui, path)
            self.assertEqual(config.backend, "apps_script")
            self.assertEqual(Config.load(path).columns, DEFAULT_COLUMNS)
            self.assertNotIn("Путь к JSON-ключу", output.getvalue())
            self.assertNotIn(TOKEN, output.getvalue())

    def test_setup_reuses_sheet_url_even_if_saved_token_is_invalid(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"spreadsheet_url": URL, "backend": "apps_script",
                                        "script_url": SCRIPT_URL, "script_token": "AKfy-wrong-id"}))
            ui = TerminalUI()
            ui.console = Console(file=io.StringIO(), no_color=True)
            with patch.object(ui, "ask", side_effect=[URL, SCRIPT_URL]) as ask, \
                 patch.object(ui, "ask_secret", return_value=TOKEN):
                config = first_setup(ui, path)
            self.assertEqual(ask.call_args_list[0].kwargs["default"], URL)
            self.assertEqual(config.script_token, TOKEN)


class ScriptClientTests(unittest.TestCase):
    def response(self, body):
        return Mock(json=Mock(return_value=body))

    def test_token_is_sent_in_post_body_not_url(self):
        with patch("leetcode_table_bot.apps_script.requests.post", return_value=self.response({"ok": True, "data": {}})) as post:
            ScriptClient(script_config()).call("metadata")
        self.assertEqual(post.call_args.args, (SCRIPT_URL,))
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["token"], TOKEN)
        self.assertEqual(body["worksheet_id"], 0)
        self.assertEqual(body["spreadsheet_id"], "example")
        self.assertEqual(post.call_args.kwargs["timeout"], (10, 30))

    def test_wrong_token_message_is_visible(self):
        with patch("leetcode_table_bot.apps_script.requests.post", return_value=self.response({"ok": False, "error": "Неверный токен"})):
            with self.assertRaisesRegex(BotError, "Неверный токен"):
                ScriptClient(script_config()).call("metadata")

    def test_html_login_page_explains_deployment_setting(self):
        response = Mock()
        response.json.side_effect = ValueError("html")
        with patch("leetcode_table_bot.apps_script.requests.post", return_value=response):
            with self.assertRaisesRegex(BotError, "Anyone"):
                ScriptClient(script_config()).call("metadata")

    def test_timeout_is_not_retried_and_does_not_print_secret(self):
        with patch("leetcode_table_bot.apps_script.requests.post", side_effect=requests.Timeout) as post:
            with self.assertRaises(BotError) as error:
                ScriptClient(script_config()).call("append", notes="private note")
        post.assert_called_once()
        self.assertNotIn(TOKEN, str(error.exception))
        self.assertNotIn("private note", str(error.exception))

    def test_google_auth_http_errors_explain_anonymous_deployment(self):
        for status in (401, 403):
            response = requests.Response()
            response.status_code = status
            with self.subTest(status=status), patch("leetcode_table_bot.apps_script.requests.post",
                                                   side_effect=requests.HTTPError(response=response)) as post:
                with self.assertRaisesRegex(BotError, f"HTTP {status}") as error:
                    ScriptClient(script_config()).call("metadata")
            self.assertIn("Anyone", str(error.exception))
            self.assertIn("без входа", str(error.exception))
            self.assertNotIn(TOKEN, str(error.exception))
            post.assert_called_once()

    def test_missing_deployment_has_specific_message(self):
        response = requests.Response()
        response.status_code = 404
        with patch("leetcode_table_bot.apps_script.requests.post", side_effect=requests.HTTPError(response=response)):
            with self.assertRaisesRegex(BotError, "не найдено.*404"):
                ScriptClient(script_config()).call("metadata")

    def test_tls_error_is_not_reported_as_a_deployment_permission_error(self):
        with patch("leetcode_table_bot.apps_script.requests.post", side_effect=requests.exceptions.SSLError):
            with self.assertRaisesRegex(BotError, "HTTPS-сертификат"):
                ScriptClient(script_config()).call("metadata")

    def test_malformed_response_does_not_crash(self):
        for body in [[], {"ok": "yes", "data": {}}, {"ok": True}, {"ok": True, "data": []}]:
            with self.subTest(body=body), patch("leetcode_table_bot.apps_script.requests.post", return_value=self.response(body)):
                with self.assertRaises(BotError):
                    ScriptClient(script_config()).call("read")


class ScriptRepositoryTests(unittest.TestCase):
    def make_repository(self):
        with patch.object(ScriptClient, "call", return_value={"title": "Лист1", "col_count": 26}):
            return ScriptRepository(script_config())

    def test_script_connection_does_not_open_json_key(self):
        with patch.object(ScriptClient, "call", return_value={"title": "Лист1", "col_count": 26}), \
             patch("gspread.service_account_from_dict") as google:
            repository = connect(script_config(), Path("/missing/config.json"), Path("/missing/state"))
        self.assertEqual(repository.worksheet.title, "Лист1")
        google.assert_not_called()

    def test_find_existing_entry_works_through_script(self):
        repository = self.make_repository()
        with patch.object(repository.client, "call", return_value={"rows": [["headers"], ["228", "Summary Ranges", "старое", "link"]]}):
            self.assertEqual(repository.find_entries(228), [ExistingEntry(2, "старое", 228)])

    def test_append_passes_problem_and_literal_note(self):
        repository = self.make_repository()
        with patch.object(repository.client, "call", return_value={}) as call:
            repository.append(Problem(228, "Summary Ranges", "summary-ranges"), "=literal\nвторая строка")
        call.assert_called_once_with("append", number=228, title="Summary Ranges", notes="=literal\nвторая строка", url="https://leetcode.com/problems/summary-ranges/")

    def test_update_validates_number_on_server(self):
        repository = self.make_repository()
        with patch.object(repository.client, "call", return_value={}) as call:
            repository.add_notes(ExistingEntry(2, "старое", 228), "новое")
        call.assert_called_once_with("add_notes", row=2, number=228, notes="новое")


if __name__ == "__main__":
    unittest.main()
