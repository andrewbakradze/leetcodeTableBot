import io
import json
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from rich.console import Console

from leetcode_table_bot.config import BotError, DEFAULT_HEADERS
from leetcode_table_bot.leetcode import Problem, Snapshot
from leetcode_table_bot.sync import PendingStore, SyncEngine, run_sync
from leetcode_table_bot.ui import TerminalUI
from test_core import config

ONE = Problem(1, "Two Sum", "two-sum", "Easy")
TWO = Problem(228, "Summary Ranges", "summary-ranges", "Easy")


class FakeRepository:
    def __init__(self, cfg):
        self.config = cfg
        self.worksheet = SimpleNamespace(title="Лист1", col_count=26)
        self.rows = [list(DEFAULT_HEADERS)]
        self.append = Mock(side_effect=self._append)

    def read_rows(self):
        return [list(row) for row in self.rows]

    def _append(self, problem, notes, resolve="нет"):
        self.rows.append([str(problem.number), problem.title, notes, problem.url, resolve])


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "pending.json"

    def test_completed_note_survives_restart(self):
        store = PendingStore(self.path)
        store.discover((ONE,))
        store.save(ONE, "строка 1\nстрока 2", "может быть")
        restored = PendingStore(self.path).pending()
        self.assertEqual(restored[0].problem, ONE)
        self.assertEqual(restored[0].notes, "строка 1\nстрока 2")
        self.assertEqual(restored[0].resolve, "может быть")

    def test_old_queue_without_resolve_is_migrated(self):
        self.path.write_text(json.dumps({"pending": [{"problem": {"number": 1, "title": "Two Sum", "slug": "two-sum"}, "notes": "старый текст"}]}))
        restored = PendingStore(self.path).pending()[0]
        self.assertEqual(restored.notes, "старый текст")
        self.assertIsNone(restored.resolve)

    def test_task_remains_when_it_falls_out_of_recent_history(self):
        store = PendingStore(self.path)
        store.discover((ONE,))
        store.discover((TWO,))
        self.assertEqual([item.problem for item in store.pending()], [ONE, TWO])

    def test_duplicate_acceptances_do_not_duplicate_queue(self):
        store = PendingStore(self.path)
        store.discover((ONE, ONE))
        store.discover((ONE,))
        self.assertEqual(len(store.pending()), 1)

    def test_failed_queue_write_does_not_discard_previous_note(self):
        store = PendingStore(self.path)
        store.save(ONE, "старое")
        with patch("leetcode_table_bot.sync.write_json", side_effect=OSError):
            with self.assertRaises(OSError):
                store.save(ONE, "новое")
        self.assertEqual(store.pending()[0].notes, "старое")

    def test_corrupted_queue_is_preserved_and_reported(self):
        self.path.write_text("broken")
        with self.assertRaises(BotError):
            PendingStore(self.path)
        self.assertEqual(self.path.read_text(), "broken")


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state_dir = Path(self.directory.name)
        self.cfg = config()
        self.repository = FakeRepository(self.cfg)
        self.leetcode = Mock()
        self.leetcode.resolve.side_effect = lambda problem: problem
        self.leetcode.fetch_snapshot.return_value = Snapshot((ONE, TWO), 2, True)
        self.engine = SyncEngine(self.cfg, self.repository, self.leetcode, self.state_dir)
        self.ui = TerminalUI()
        self.output = io.StringIO()
        self.ui.console = Console(file=self.output, no_color=True, width=100)
        self.ui.resolve_choice = Mock(return_value="нет")

    def test_startup_compares_all_available_tasks_to_table(self):
        self.repository.rows.append(["1", "Two Sum", "old", ONE.url])
        self.engine.poll_once()
        self.assertEqual([item.problem for item in self.engine.store.pending()], [TWO])

    def test_recorded_task_without_number_is_matched_by_url(self):
        self.repository.rows.append(["", "Two Sum", "", ONE.url])
        self.engine.poll_once()
        self.assertEqual([item.problem for item in self.engine.store.pending()], [TWO])

    def test_incomplete_history_is_reported_once(self):
        self.leetcode.fetch_snapshot.return_value = Snapshot((ONE,), 3, False)
        self.engine.poll_once()
        self.engine.poll_once()
        messages = []
        while not self.engine.messages.empty():
            messages.append(self.engine.messages.get())
        self.assertEqual(sum("неполная" in message for message in messages), 1)

    def test_google_failure_does_not_lose_newly_discovered_tasks(self):
        with patch.object(self.repository, "read_rows", side_effect=BotError("offline")):
            with self.assertRaises(BotError):
                self.engine.poll_once()
        self.assertEqual(len(PendingStore(self.engine.store.path).pending()), 2)

    def test_different_table_or_profile_has_separate_queue(self):
        other = config(leetcode_username="another")
        engine = SyncEngine(other, self.repository, self.leetcode, self.state_dir)
        self.assertNotEqual(engine.store.path, self.engine.store.path)

    def test_only_difficulties_are_requested_and_completed_item_is_removed(self):
        self.engine.store.discover((TWO,))
        def notes():
            self.engine.stop_event.set()
            return "Пустой массив\nПереполнение"
        with patch.object(self.engine, "start"), patch.object(self.ui, "notes", side_effect=notes), patch.object(self.ui, "ask") as ask:
            run_sync(self.ui, self.engine)
        ask.assert_not_called()
        self.repository.append.assert_called_once_with(TWO, "Пустой массив\nПереполнение", "нет")
        self.ui.resolve_choice.assert_called_once()
        self.assertEqual(self.engine.store.pending(), [])
        self.assertIn("#228", self.output.getvalue())
        self.assertIn("Summary Ranges", self.output.getvalue())

    def test_restored_note_does_not_require_retyping(self):
        self.engine.store.save(TWO, "восстановленный текст", "ДА")
        def append(problem, notes, resolve):
            self.repository._append(problem, notes, resolve)
            self.engine.stop_event.set()
        self.repository.append.side_effect = append
        with patch.object(self.engine, "start"), patch.object(self.ui, "notes") as notes:
            run_sync(self.ui, self.engine)
        notes.assert_not_called()
        self.repository.append.assert_called_once_with(TWO, "восстановленный текст", "ДА")
        self.ui.resolve_choice.assert_not_called()

    def test_failed_write_keeps_completed_note(self):
        self.engine.store.discover((TWO,))
        def append(problem, notes, resolve):
            self.engine.stop_event.set()
            raise BotError("network error")
        self.repository.append.side_effect = append
        with patch.object(self.engine, "start"), patch.object(self.ui, "notes", return_value="не потерять"):
            run_sync(self.ui, self.engine)
        self.assertEqual(PendingStore(self.engine.store.path).pending()[0].notes, "не потерять")
        self.assertEqual(PendingStore(self.engine.store.path).pending()[0].resolve, "нет")

    def test_resolve_is_requested_after_completed_difficulties(self):
        self.engine.store.discover((TWO,))
        order = []
        def notes():
            order.append("notes")
            return "трудности"
        def choice():
            order.append("resolve")
            return "может быть"
        def append(problem, notes, resolve):
            order.append("append")
            self.engine.stop_event.set()
        self.repository.append.side_effect = append
        self.ui.resolve_choice.side_effect = choice
        with patch.object(self.engine, "start"), patch.object(self.ui, "notes", side_effect=notes):
            run_sync(self.ui, self.engine)
        self.assertEqual(order, ["notes", "resolve", "append"])
        self.repository.append.assert_called_once_with(TWO, "трудности", "может быть")

    def test_interrupt_at_resolve_keeps_finished_difficulties(self):
        self.engine.store.discover((TWO,))
        self.ui.resolve_choice.side_effect = KeyboardInterrupt
        with patch.object(self.engine, "start"), patch.object(self.ui, "notes", return_value="не потерять"), self.assertRaises(KeyboardInterrupt):
            run_sync(self.ui, self.engine)
        restored = PendingStore(self.engine.store.path).pending()[0]
        self.assertEqual(restored.notes, "не потерять")
        self.assertIsNone(restored.resolve)
        self.repository.append.assert_not_called()

    def test_ambiguous_write_is_not_sent_again_after_restart(self):
        self.engine.store.save(TWO, "notes")
        self.repository._append(TWO, "notes")  # сервер сохранил строку, но ответ потерялся
        restored = SyncEngine(self.cfg, self.repository, self.leetcode, self.state_dir)
        restored.poll_once()
        self.assertNotIn(TWO.slug, [item.problem.slug for item in restored.store.pending()])
        self.repository.append.assert_not_called()

    def test_resolved_number_already_in_table_is_not_prompted(self):
        candidate = replace(TWO, number=None)
        self.engine.store.discover((candidate,))
        self.repository.rows.append(["228", "Summary Ranges", "old"])
        def resolve(problem):
            self.engine.stop_event.set()
            return TWO
        self.leetcode.resolve.side_effect = resolve
        with patch.object(self.engine, "start"), patch.object(self.ui, "notes") as notes:
            run_sync(self.ui, self.engine)
        notes.assert_not_called()
        self.repository.append.assert_not_called()

    def test_background_keeps_polling_while_user_writes_note(self):
        self.engine.store.discover((ONE,))
        detected = threading.Event()
        calls = 0
        def snapshot():
            nonlocal calls
            calls += 1
            if calls >= 2:
                detected.set()
                return Snapshot((ONE, TWO), 2, True)
            return Snapshot((ONE,), 1, True)
        self.leetcode.fetch_snapshot.side_effect = snapshot
        def notes():
            self.assertTrue(detected.wait(1), "фоновая проверка остановилась во время ввода заметки")
            self.engine.stop_event.set()
            return "долго вводил заметку"
        with patch("leetcode_table_bot.sync.POLL_INTERVAL", 0.02), patch.object(self.ui, "notes", side_effect=notes):
            run_sync(self.ui, self.engine)
        self.assertIn(TWO.slug, [item.problem.slug for item in self.engine.store.pending()])

    def test_normal_poll_interval_is_exactly_one_minute(self):
        with patch.object(self.engine, "poll_once"), patch("leetcode_table_bot.sync.time.monotonic", return_value=100), patch.object(self.engine.stop_event, "wait", return_value=True) as wait:
            self.engine._run()
        wait.assert_called_once_with(60)
