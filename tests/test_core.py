import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from leetcode_table_bot.config import BotError, Config, DEFAULT_COLUMNS, DEFAULT_HEADERS, column_letter, column_number, parse_spreadsheet_url, validate_columns, write_json
from leetcode_table_bot.leetcode import Problem, parse_username
from leetcode_table_bot.sheets import detect_columns, recorded_tasks

URL = "https://docs.google.com/spreadsheets/d/example/edit?gid=0#gid=0"
SCRIPT_URL = "https://script.google.com/macros/s/example/exec"


def config(**kwargs):
    return Config(URL, script_url=SCRIPT_URL, script_token="a" * 64, columns=dict(DEFAULT_COLUMNS), **kwargs)


class ConfigTests(unittest.TestCase):
    def test_sheet_url_and_fragment_select_tab(self):
        self.assertEqual(parse_spreadsheet_url(URL).worksheet_id, 0)
        self.assertEqual(parse_spreadsheet_url(URL.replace("#gid=0", "#gid=27")).worksheet_id, 27)
        self.assertIsNone(parse_spreadsheet_url(URL.split("?")[0]).worksheet_id)

    def test_rejects_impostor_url(self):
        with self.assertRaises(BotError):
            parse_spreadsheet_url(URL.replace("docs.google.com", "docs.google.com.evil.test"))

    def test_migrates_old_config_without_changing_google_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            data = {"spreadsheet_url": URL, "backend": "apps_script", "credentials_file": "unused.json",
                    "script_url": SCRIPT_URL, "script_token": "a" * 64, "columns": DEFAULT_COLUMNS}
            path.write_text(json.dumps(data))
            result = Config.load(path)
            result.save(path)
            self.assertEqual(result.leetcode_username, "1-andro1d-1")
            self.assertEqual(result.script_token, "a" * 64)
            self.assertNotIn("backend", json.loads(path.read_text()))
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_token_is_not_deployment_id_and_hidden_in_repr(self):
        self.assertNotIn("a" * 64, repr(config()))
        with self.assertRaises(BotError):
            Config(URL, script_url=SCRIPT_URL, script_token="AKfy" + "a" * 60)

    def test_validates_profile_link(self):
        self.assertEqual(parse_username("https://leetcode.com/u/1-andro1d-1/"), "1-andro1d-1")
        self.assertEqual(parse_username("1-andro1d-1"), "1-andro1d-1")
        with self.assertRaises(BotError):
            parse_username("https://leetcode.com.evil.test/u/test/")

    def test_duplicate_column_mapping_is_rejected(self):
        with self.assertRaises(BotError):
            validate_columns({"number": 1, "title": 1, "notes": 3, "url": 4})
        self.assertEqual(column_number("AA"), 27)
        self.assertEqual(column_letter(27), "AA")

    def test_atomic_failure_does_not_destroy_previous_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.json"
            write_json(path, {"old": True})
            with patch("leetcode_table_bot.config.os.replace", side_effect=OSError):
                with self.assertRaises(OSError):
                    write_json(path, {"old": False})
            self.assertEqual(json.loads(path.read_text()), {"old": True})


class SheetTests(unittest.TestCase):
    def test_detects_user_template(self):
        self.assertEqual(detect_columns(DEFAULT_HEADERS), DEFAULT_COLUMNS)

    def test_reordered_russian_headers(self):
        self.assertEqual(detect_columns(["Ссылка", "Resolve:", "Мои сложности", "Номер задачи", "Название задачи"]), {"url": 1, "resolve": 2, "notes": 3, "number": 4, "title": 5})

    def test_previous_four_column_map_remains_loadable(self):
        self.assertEqual(validate_columns({"number": 1, "title": 2, "notes": 3, "url": 4}), {"number": 1, "title": 2, "notes": 3, "url": 4})

    def test_existing_number_or_link_counts_as_already_recorded(self):
        rows = [DEFAULT_HEADERS, ["228.0", "name"], ["", "name", "", "https://leetcode.com/problems/two-sum/description/"], []]
        numbers, slugs = recorded_tasks(rows, config())
        self.assertEqual(numbers, {228})
        self.assertEqual(slugs, {"two-sum"})

    def test_task_validation_rejects_non_numeric_and_unsafe_metadata(self):
        with self.assertRaises(BotError):
            Problem.from_data({"number": "LCP 01", "title": "name", "slug": "name"})
        with self.assertRaises(BotError):
            Problem.from_data({"number": 1, "title": "name", "slug": "../../evil"})
