import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from types import SimpleNamespace

from rich.console import Console

from leetcode_table_bot.cli import build_parser, configure_columns, main
from leetcode_table_bot.ui import TerminalUI
from leetcode_table_bot.config import Config, DEFAULT_COLUMNS, DEFAULT_HEADERS
from test_core import config


class InterfaceTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.ui = TerminalUI()
        self.ui.console = Console(file=self.output, no_color=True)

    def test_old_manual_modes_are_removed(self):
        with patch("sys.stderr", new=io.StringIO()):
            for flag in ("--demo", "--google-api", "--apps-script"):
                with self.subTest(flag=flag), self.assertRaises(SystemExit):
                    build_parser().parse_args([flag])

    def test_multiline_difficulties_and_blank_lines(self):
        with patch.object(self.ui.console, "input", side_effect=["первая", "", "вторая", "."]):
            self.assertEqual(self.ui.notes(), "первая\n\nвторая")

    def test_later_and_skip(self):
        with patch.object(self.ui.console, "input", return_value="/later"):
            self.assertIsNone(self.ui.notes())
        with patch.object(self.ui.console, "input", return_value="/skip"):
            self.assertEqual(self.ui.notes(), "")

    def test_ctrl_c_has_no_traceback(self):
        with patch("leetcode_table_bot.cli.run", side_effect=KeyboardInterrupt), patch("leetcode_table_bot.ui.TerminalUI", return_value=self.ui):
            self.assertEqual(main([]), 0)

    def test_limit_preserves_previous_lines(self):
        with patch.object(self.ui.console, "input", side_effect=["x" * 50000, "extra", "."]):
            self.assertEqual(len(self.ui.notes()), 50000)

    def test_resolve_options_accept_numbers_and_exact_labels(self):
        for value, expected in [("1", "ДА"), ("2", "может быть"), ("3", "нет"), ("да", "ДА"), ("может быть", "может быть"), ("Нет", "нет")]:
            with self.subTest(value=value), patch.object(self.ui.console, "input", return_value=value):
                self.assertEqual(self.ui.resolve_choice(), expected)

    def test_resolve_reprompts_on_invalid_option(self):
        with patch.object(self.ui.console, "input", side_effect=["что-нибудь", "2"]):
            self.assertEqual(self.ui.resolve_choice(), "может быть")

    def test_new_resolve_column_is_added_to_saved_mapping_without_setup_prompt(self):
        cfg = config()
        cfg.columns = {key: value for key, value in DEFAULT_COLUMNS.items() if key != "resolve"}
        repository = SimpleNamespace(config=cfg, read_rows=Mock(return_value=[list(DEFAULT_HEADERS)]), worksheet=SimpleNamespace(col_count=26))
        with tempfile.TemporaryDirectory() as directory, patch.object(self.ui, "ask") as ask:
            path = Path(directory) / "config.json"
            configure_columns(self.ui, repository, path)
            self.assertEqual(Config.load(path).columns["resolve"], 5)
            ask.assert_not_called()
