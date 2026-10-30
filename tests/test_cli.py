from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from rich.console import Console

from leetcode_table_bot.cli import configure_columns, main, run
from leetcode_table_bot.config import BotError, Config, DEFAULT_COLUMNS, DEFAULT_HEADERS
from leetcode_table_bot.leetcode import Problem
from leetcode_table_bot.sheets import SheetRepository, connect
from leetcode_table_bot.ui import TerminalUI

URL = "https://docs.google.com/spreadsheets/d/test-sheet/edit#gid=0"
PROBLEM = Problem(228, "Summary Ranges", "summary-ranges", "Easy")


class InteractiveTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config_path = Path(self.directory.name) / "config.json"
        self.config = Config(URL, columns=dict(DEFAULT_COLUMNS))
        self.config.save(self.config_path)
        self.output = io.StringIO()
        self.ui = TerminalUI()
        self.ui.console = Console(file=self.output, width=100, no_color=True)
        self.sheet = Mock(col_count=10, title="Лист1")
        self.sheet.get_all_values.return_value = [DEFAULT_HEADERS]
        self.repository = SheetRepository(self.sheet, self.config)

    def run_script(self, inputs):
        args = SimpleNamespace(config=self.config_path, setup=False)
        with patch.object(self.ui.console, "input", side_effect=inputs), \
             patch("leetcode_table_bot.cli.connect", return_value=self.repository), \
             patch("leetcode_table_bot.cli.LeetCodeClient.get_problem", return_value=PROBLEM):
            return run(self.ui, args, (BotError, OSError))

    def test_full_flow_adds_multiline_note_and_backup(self):
        self.assertEqual(self.run_script(["228", "Пустой массив.", "", "Переполнение.", ".", "/exit"]), 0)
        notes = "Пустой массив.\n\nПереполнение."
        self.sheet.append_row.assert_called_once()
        self.assertEqual(self.sheet.append_row.call_args.args[0], [228, "Summary Ranges", notes, PROBLEM.url])
        draft = json.loads((self.config_path.parent / ".leetcode-table-bot/last_entry.json").read_text())
        self.assertEqual(draft["notes"], notes)
        self.assertIn("сохранена", self.output.getvalue())

    def test_cancel_does_not_write(self):
        self.run_script(["228", "незавершённая заметка", "/cancel", "q"])
        self.sheet.append_row.assert_not_called()

    def test_skip_saves_without_notes(self):
        self.run_script(["228", "/skip", "q"])
        self.assertEqual(self.sheet.append_row.call_args.args[0][2], "")

    def test_duplicate_appends_to_previous_notes(self):
        self.sheet.get_all_values.return_value = [DEFAULT_HEADERS, ["228", "Summary Ranges", "первая заметка"]]
        self.sheet.row_values.return_value = ["228", "Summary Ranges", "первая заметка"]
        self.run_script(["228", "дополнить", "вторая заметка", ".", "q"])
        self.sheet.append_row.assert_not_called()
        self.assertEqual(self.sheet.update.call_args.kwargs["values"], [["первая заметка\n\nвторая заметка"]])

    def test_failed_google_write_keeps_draft_and_continues(self):
        self.sheet.append_row.side_effect = OSError("network failed")
        self.run_script(["228", "важный текст", ".", "/draft", "q"])
        draft = self.config_path.parent / ".leetcode-table-bot/last_entry.json"
        self.assertEqual(json.loads(draft.read_text())["notes"], "важный текст")
        self.assertIn("Перед повтором проверьте таблицу", self.output.getvalue())
        self.assertIn("Локальная копия", self.output.getvalue())

    def test_bad_input_does_not_end_session(self):
        self.run_script(["oops", "228", "/skip", "q"])
        self.sheet.append_row.assert_called_once()
        self.assertIn("положительный номер", self.output.getvalue())

    def test_blocked_leetcode_can_save_manually_entered_metadata(self):
        args = SimpleNamespace(config=self.config_path, setup=False)
        inputs = ["1493", "да", "https://leetcode.com/problems/longest-subarray-of-1s-after-deleting-one-element/description/",
                  "Longest Subarray of 1's After Deleting One Element", "Сложно обработать удаление одного элемента", ".", "q"]
        with patch.object(self.ui.console, "input", side_effect=inputs), \
             patch("leetcode_table_bot.cli.connect", return_value=self.repository), \
             patch("leetcode_table_bot.cli.LeetCodeClient.get_problem", side_effect=BotError("HTTP 403")):
            self.assertEqual(run(self.ui, args, (BotError, OSError)), 0)
        self.assertEqual(self.sheet.append_row.call_args.args[0], [1493, "Longest Subarray of 1's After Deleting One Element",
                         "Сложно обработать удаление одного элемента", "https://leetcode.com/problems/longest-subarray-of-1s-after-deleting-one-element/"])

    def test_manual_fallback_can_be_cancelled_without_writing(self):
        args = SimpleNamespace(config=self.config_path, setup=False)
        with patch.object(self.ui.console, "input", side_effect=["1493", "отмена", "q"]), \
             patch("leetcode_table_bot.cli.connect", return_value=self.repository), \
             patch("leetcode_table_bot.cli.LeetCodeClient.get_problem", side_effect=BotError("HTTP 403")):
            run(self.ui, args, (BotError, OSError))
        self.sheet.append_row.assert_not_called()

    def test_template_detected_without_prompt(self):
        self.config.columns = {}
        with patch.object(self.ui, "ask") as prompt:
            configure_columns(self.ui, self.repository, self.config_path)
        prompt.assert_not_called()
        self.assertEqual(Config.load(self.config_path).columns, DEFAULT_COLUMNS)
        self.sheet.update.assert_not_called()

    def test_first_setup_keeps_url_if_credentials_missing(self):
        with patch("leetcode_table_bot.cli.connect", side_effect=BotError("ключ не скачан")), \
             patch.object(self.ui.console, "input", side_effect=[URL, "missing.json"]):
            with self.assertRaisesRegex(BotError, "ключ не скачан"):
                run(self.ui, SimpleNamespace(config=self.config_path, setup=True, google_api=True), (BotError,))
        self.assertEqual(Config.load(self.config_path).spreadsheet_url, URL)

    def test_note_limit_keeps_previous_lines(self):
        with patch.object(self.ui.console, "input", side_effect=["x" * 50000, "extra", "."]):
            self.assertEqual(len(self.ui.notes()), 50000)
        self.assertIn("Лимит заметки", self.output.getvalue())

    def test_markup_in_note_is_literal(self):
        self.ui.show_draft(PROBLEM, "[red]это мой текст[/red]")
        self.assertIn("[red]это мой текст[/red]", self.output.getvalue())

    def test_ctrl_c_returns_without_traceback(self):
        with patch("leetcode_table_bot.cli.run", side_effect=KeyboardInterrupt), \
             patch("leetcode_table_bot.ui.TerminalUI", return_value=self.ui):
            self.assertEqual(main(["--config", str(self.config_path)]), 0)


class GoogleConnectionTests(unittest.TestCase):
    def test_service_account_uses_only_sheets_scope_and_gid_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "credentials.json").write_text(json.dumps({"type": "service_account"}))
            config = Config(URL)
            with patch("gspread.service_account_from_dict") as auth:
                connect(config, root / "config.json", root / "state")
            self.assertEqual(auth.call_args.kwargs["scopes"], ["https://www.googleapis.com/auth/spreadsheets"])
            auth.return_value.open_by_key.return_value.get_worksheet_by_id.assert_called_once_with(0)

    def test_oauth_uses_config_relative_path_and_private_token(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "credentials.json").write_text(json.dumps({"installed": {}}))
            state_dir = root / "state"
            def authorize(**kwargs):
                Path(kwargs["authorized_user_filename"]).write_text("{}")
                return Mock()
            with patch("gspread.oauth", side_effect=authorize) as oauth:
                connect(Config(URL), root / "config.json", state_dir)
            self.assertEqual(oauth.call_args.kwargs["credentials_filename"], str(root / "credentials.json"))
            self.assertEqual((state_dir / "authorized_user.json").stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
