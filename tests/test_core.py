from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from leetcode_table_bot.config import (
    BotError, Config, DEFAULT_HEADERS, column_letter, column_number,
    parse_spreadsheet_url, validate_columns, write_json,
)
from leetcode_table_bot.leetcode import (
    LeetCodeClient, parse_catalog, parse_problem_number, parse_problem_url,
    parse_mirror_index, parse_mirror_problem,
)
from leetcode_table_bot.sheets import SheetRepository, detect_columns

URL = "https://docs.google.com/spreadsheets/d/test-sheet_123/edit?hl=ru&gid=0#gid=0"
CATALOG = {"stat_status_pairs": [
    {"stat": {"frontend_question_id": 228, "question_id": 999,
              "question__title": "Summary Ranges", "question__title_slug": "summary-ranges"}, "difficulty": {"level": 1}},
    {"stat": {"frontend_question_id": "LCP 01", "question_id": 228,
              "question__title": "Other", "question__title_slug": "other"}},
    {"stat": {"frontend_question_id": 1, "question__title": "Two Sum", "question__title_slug": "two-sum"},
     "difficulty": {"level": 1}},
]}


class ConfigTests(unittest.TestCase):
    def test_url_supports_user_example_and_zero_gid(self):
        link = parse_spreadsheet_url(URL)
        self.assertEqual((link.spreadsheet_id, link.worksheet_id), ("test-sheet_123", 0))

    def test_fragment_selects_visible_tab(self):
        self.assertEqual(parse_spreadsheet_url(URL.replace("#gid=0", "#gid=27")).worksheet_id, 27)

    def test_missing_gid_means_first_sheet(self):
        self.assertIsNone(parse_spreadsheet_url("https://docs.google.com/spreadsheets/d/abc/edit").worksheet_id)

    def test_rejects_untrusted_host_and_invalid_gid(self):
        for url in [URL.replace("docs.google.com", "docs.google.com.evil.test"),
                    URL.replace("https:", "http:"), URL.replace("gid=0", "gid=oops")]:
            with self.subTest(url=url), self.assertRaises(BotError):
                parse_spreadsheet_url(url)

    def test_config_roundtrip_and_private_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config = Config(URL, columns={"number": 1, "title": 2, "notes": 3, "url": 4})
            config.save(path)
            self.assertEqual(Config.load(path), config)
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_invalid_config_has_actionable_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(BotError, "--setup"):
                Config.load(path)

    def test_invalid_column_mapping_is_rejected(self):
        for columns in [None, {}, {"number": 1, "title": 2, "notes": 3, "url": 3},
                        {"number": True, "title": 2, "notes": 3, "url": 4},
                        {"number": 0, "title": 2, "notes": 3, "url": 4}]:
            with self.subTest(columns=columns), self.assertRaises(BotError):
                validate_columns(columns)

    def test_column_letters_beyond_z(self):
        for index, letter in [(1, "A"), (26, "Z"), (27, "AA"), (18278, "ZZZ")]:
            self.assertEqual(column_letter(index), letter)
            self.assertEqual(column_number(letter.lower()), index)

    def test_header_row_cannot_be_bool(self):
        with self.assertRaises(BotError):
            Config(URL, header_row=True)

    def test_failed_atomic_save_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            write_json(path, {"value": "старое"})
            with patch("leetcode_table_bot.config.os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    write_json(path, {"value": "новое"})
            self.assertEqual(json.loads(path.read_text())["value"], "старое")
            self.assertEqual(list(Path(directory).iterdir()), [path])


class LeetCodeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cache = Path(self.directory.name) / "cache.json"

    def test_uses_frontend_id_not_internal_id(self):
        problems = parse_catalog(CATALOG)
        self.assertEqual(problems[228].title, "Summary Ranges")
        self.assertEqual(problems[228].url, "https://leetcode.com/problems/summary-ranges/")
        self.assertNotIn(999, problems)

    def test_validates_number(self):
        self.assertEqual(parse_problem_number(" 0228 "), 228)
        for value in ["0", "-1", "abc", "228.0", "", "=1", "１２", "1/2"]:
            with self.subTest(value=value), self.assertRaises(BotError):
                parse_problem_number(value)

    def test_invalid_catalog_and_unsafe_slug_are_rejected(self):
        for payload in [None, [], {"stat_status_pairs": []},
                        {"stat_status_pairs": [{"stat": {"frontend_question_id": 1,
                         "question__title": "test", "question__title_slug": "../../evil"}}]}]:
            with self.subTest(payload=payload), self.assertRaises(BotError):
                parse_catalog(payload)

    def test_fetches_and_caches_catalog(self):
        with patch("leetcode_table_bot.leetcode.urlopen", return_value=io.BytesIO(json.dumps(CATALOG).encode())) as request:
            problem = LeetCodeClient(self.cache).get_problem(228)
        self.assertEqual(problem.number, 228)
        self.assertEqual(request.call_args.kwargs["timeout"], 25)
        self.assertEqual(json.loads(self.cache.read_text())["catalog"], CATALOG)

    def test_fresh_cache_avoids_network(self):
        write_json(self.cache, {"fetched_at": time.time(), "catalog": CATALOG})
        with patch("leetcode_table_bot.leetcode.urlopen") as request:
            self.assertEqual(LeetCodeClient(self.cache).get_problem(1).title, "Two Sum")
        request.assert_not_called()

    def test_stale_cache_works_offline_with_warning(self):
        write_json(self.cache, {"fetched_at": time.time() - 90000, "catalog": CATALOG})
        client = LeetCodeClient(self.cache)
        with patch("leetcode_table_bot.leetcode.urlopen", side_effect=URLError("offline")):
            self.assertEqual(client.get_problem(228).number, 228)
        self.assertIn("кеша", client.warning)

    def test_missing_problem_refreshes_even_fresh_cache(self):
        write_json(self.cache, {"fetched_at": time.time(), "catalog": CATALOG})
        with patch("leetcode_table_bot.leetcode.urlopen", return_value=io.BytesIO(json.dumps(CATALOG).encode())) as request:
            with self.assertRaisesRegex(BotError, "не найдена"):
                LeetCodeClient(self.cache).get_problem(9999)
        request.assert_called_once()

    def test_corrupted_cache_is_ignored(self):
        self.cache.write_text("not json", encoding="utf-8")
        client = LeetCodeClient(self.cache)
        self.assertEqual(client.problems, {})

    def test_cloudflare_and_rate_limit_are_readable(self):
        for code in [403, 429]:
            with self.subTest(code=code), patch("leetcode_table_bot.leetcode.urlopen", side_effect=HTTPError("url", code, "error", {}, None)):
                with self.assertRaisesRegex(BotError, str(code)):
                    LeetCodeClient(self.cache).get_problem(228)

    def test_invalid_json_is_readable(self):
        with patch("leetcode_table_bot.leetcode.urlopen", return_value=io.BytesIO(b"<html>error</html>")):
            with self.assertRaisesRegex(BotError, "некорректный ответ"):
                LeetCodeClient(self.cache).get_problem(228)

    def test_manual_link_is_canonicalized(self):
        self.assertEqual(parse_problem_url("https://leetcode.com/problems/two-sum/description/?envType=test"), "two-sum")
        for value in ["http://leetcode.com/problems/two-sum/", "https://leetcode.com.evil.test/problems/two-sum/", "https://example.test/"]:
            with self.subTest(value=value), self.assertRaises(BotError):
                parse_problem_url(value)

    def test_mirror_index_uses_exact_number_and_safe_repository_path(self):
        text = "| 1493 | [Longest Subarray of 1's After Deleting One Element](/solution/1400-1499/1493.Longest%20Subarray%20of%201%27s%20After%20Deleting%20One%20Element/README_EN.md) | `Array` | Medium | |"
        entries = parse_mirror_index(text)
        self.assertEqual(entries[1493][1], "Medium")
        self.assertIn("%27", entries[1493][0])
        for unsafe in ["https://evil.test/read", "/solution/1400-1499/1.Wrong/README_EN.md", "/solution/1400-1499/1493.x%2F..%2Fevil/README_EN.md"]:
            payload = f"| 1493 | [Task]({unsafe}) | `Array` | Medium | |"
            with self.subTest(unsafe=unsafe), self.assertRaises(BotError):
                parse_mirror_index(payload)

    def test_mirror_reads_exact_slug_instead_of_guessing(self):
        text = "# [1493. Longest Subarray of 1's After Deleting One Element](https://leetcode.com/problems/longest-subarray-of-1s-after-deleting-one-element/)"
        problem = parse_mirror_problem(text, 1493, "Medium")
        self.assertEqual(problem.slug, "longest-subarray-of-1s-after-deleting-one-element")
        self.assertEqual(problem.title, "Longest Subarray of 1's After Deleting One Element")
        with self.assertRaises(BotError):
            parse_mirror_problem(text, 1, "Medium")

    def test_http_403_uses_mirror_then_persistent_cache(self):
        index = "| 1493 | [Longest Subarray of 1's After Deleting One Element](/solution/1400-1499/1493.Longest%20Subarray/README_EN.md) | `Array` | Medium | |"
        document = "# [1493. Longest Subarray of 1's After Deleting One Element](https://leetcode.com/problems/longest-subarray-of-1s-after-deleting-one-element/)"
        client = LeetCodeClient(self.cache)
        blocked = HTTPError("url", 403, "Forbidden", {}, None)
        with patch("leetcode_table_bot.leetcode.urlopen", side_effect=[blocked, io.BytesIO(index.encode()), io.BytesIO(document.encode())]) as request:
            problem = client.get_problem(1493)
        self.assertEqual(problem.number, 1493)
        self.assertIn("doocs/leetcode", client.warning)
        self.assertEqual(request.call_count, 3)
        with patch("leetcode_table_bot.leetcode.urlopen") as request:
            self.assertEqual(LeetCodeClient(self.cache).get_problem(1493), problem)
        request.assert_not_called()

    def test_does_not_repeat_blocked_primary_requests_in_same_run(self):
        client = LeetCodeClient(self.cache)
        with patch.object(client, "_refresh", side_effect=BotError("HTTP 403")) as primary, \
             patch.object(client, "_get_mirror_problem", side_effect=BotError("offline")):
            for number in (1493, 1494):
                with self.assertRaises(BotError):
                    client.get_problem(number)
        primary.assert_called_once()


class SheetTests(unittest.TestCase):
    def setUp(self):
        self.config = Config(URL, columns={"number": 1, "title": 2, "notes": 3, "url": 4})
        self.sheet = Mock(col_count=10, title="Лист1")
        self.repository = SheetRepository(self.sheet, self.config)
        self.problem = parse_catalog(CATALOG)[228]

    def test_recognizes_exact_user_template(self):
        self.assertEqual(detect_columns(DEFAULT_HEADERS), self.config.columns)

    def test_recognizes_reordered_russian_headers(self):
        self.assertEqual(detect_columns(["Ссылка", "Мои сложности", "Номер задачи", "Название задачи"]),
                         {"url": 1, "notes": 2, "number": 3, "title": 4})

    def test_append_respects_template_and_preserves_literal_notes(self):
        self.repository.append(self.problem, '=IMPORTXML("https://example.test")\nещё строка')
        values = self.sheet.append_row.call_args.args[0]
        self.assertEqual(values, [228, "Summary Ranges", '=IMPORTXML("https://example.test")\nещё строка', self.problem.url])
        self.assertEqual(self.sheet.append_row.call_args.kwargs["value_input_option"], "RAW")
        self.sheet.update.assert_not_called()

    def test_reordered_columns_are_used(self):
        self.config.columns = {"number": 4, "title": 2, "notes": 1, "url": 3}
        self.repository.append(self.problem, "заметка")
        self.assertEqual(self.sheet.append_row.call_args.args[0], ["заметка", "Summary Ranges", self.problem.url, 228])

    def test_finds_duplicates_after_header_and_ignores_blanks(self):
        self.sheet.get_all_values.return_value = [DEFAULT_HEADERS, ["228", "Summary Ranges", "старое"], [], ["228.0", "Summary Ranges", "второе"]]
        entries = self.repository.find_entries(228)
        self.assertEqual([(entry.row, entry.notes) for entry in entries], [(2, "старое"), (4, "второе")])

    def test_update_only_notes_and_preserves_concurrent_edit(self):
        self.sheet.get_all_values.return_value = [DEFAULT_HEADERS, ["228", "Summary Ranges", "первое"]]
        entry = self.repository.find_entries(228)[0]
        self.sheet.row_values.return_value = ["228", "Summary Ranges", "первое и ручное дополнение"]
        self.repository.add_notes(entry, "новое")
        self.sheet.update.assert_called_once_with(range_name="C2", values=[["первое и ручное дополнение\n\nновое"]], value_input_option="RAW")
        self.sheet.append_row.assert_not_called()

    def test_does_not_initialize_sheet_with_data_below_blank_header(self):
        self.sheet.get_all_values.return_value = [[], ["228"]]
        with self.assertRaises(BotError):
            self.repository.initialize_empty_sheet()
        self.sheet.update.assert_not_called()

    def test_initializes_empty_sheet_with_exact_template(self):
        self.sheet.get_all_values.return_value = []
        self.repository.initialize_empty_sheet()
        self.sheet.update.assert_called_once_with(range_name="A1:D1", values=[DEFAULT_HEADERS], value_input_option="RAW")

    def test_update_rejects_overlong_combined_notes(self):
        self.sheet.get_all_values.return_value = [DEFAULT_HEADERS, ["228"]]
        self.sheet.row_values.return_value = ["228", "Summary Ranges", "x" * 49999]
        with self.assertRaisesRegex(BotError, "50 000"):
            self.repository.add_notes(self.repository.find_entries(228)[0], "too long")
        self.sheet.update.assert_not_called()

    def test_moved_row_does_not_overwrite_another_problem(self):
        self.sheet.get_all_values.return_value = [DEFAULT_HEADERS, ["228"]]
        self.sheet.row_values.return_value = ["1", "Two Sum", "чужая заметка"]
        with self.assertRaisesRegex(BotError, "Строка задачи изменилась"):
            self.repository.add_notes(self.repository.find_entries(228)[0], "новое")
        self.sheet.update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
