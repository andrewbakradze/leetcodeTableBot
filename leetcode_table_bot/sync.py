"""Минутная сверка в фоне и локальная очередь задач с difficulties."""

from __future__ import annotations

import hashlib
import json
import queue
import threading
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import requests

from .config import BotError, Config, RESOLVE_OPTIONS, write_json
from .leetcode import LeetCodeClient, Problem
from .sheets import SheetRepository, recorded_tasks

POLL_INTERVAL = 60


@dataclass(frozen=True)
class Pending:
    problem: Problem
    notes: str | None = None
    resolve: str | None = None


class PendingStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.RLock()
        self.items: dict[str, Pending] = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                for item in data["pending"]:
                    problem = Problem.from_data(item["problem"])
                    notes = item["notes"]
                    if notes is not None and (not isinstance(notes, str) or len(notes) > 50000):
                        raise ValueError("invalid notes")
                    resolve = item.get("resolve")
                    if resolve is not None and resolve not in RESOLVE_OPTIONS:
                        raise ValueError("invalid resolve")
                    self.items[problem.slug] = Pending(problem, notes, resolve)
            except (OSError, ValueError, TypeError, KeyError, BotError) as error:
                raise BotError(f"Не удалось прочитать очередь {path}. Файл сохранён; исправьте его перед запуском, чтобы не потерять заметки.") from error

    def _commit(self, items: dict[str, Pending]) -> None:
        write_json(self.path, {"pending": [asdict(item) for item in items.values()]})
        self.items = items

    def discover(self, problems: tuple[Problem, ...]) -> None:
        with self.lock:
            items = dict(self.items)
            for problem in problems:
                if problem.slug not in items:
                    items[problem.slug] = Pending(problem)
            if items != self.items:
                self._commit(items)

    def remove_recorded(self, numbers: set[int], slugs: set[str]) -> None:
        with self.lock:
            items = {slug: item for slug, item in self.items.items() if slug not in slugs and item.problem.number not in numbers}
            if items != self.items:
                self._commit(items)

    def save(self, problem: Problem, notes: str | None, resolve: str | None = None) -> None:
        if resolve is not None and resolve not in RESOLVE_OPTIONS:
            raise BotError("Resolve должен быть: ДА, может быть или нет.")
        with self.lock:
            items = dict(self.items)
            items[problem.slug] = Pending(problem, notes, resolve)
            self._commit(items)

    def acknowledge(self, slug: str) -> None:
        with self.lock:
            items = dict(self.items)
            items.pop(slug, None)
            self._commit(items)

    def pending(self) -> list[Pending]:
        with self.lock:
            return list(self.items.values())


class SyncEngine:
    def __init__(self, config: Config, repository: SheetRepository, leetcode: LeetCodeClient, state_dir: Path) -> None:
        self.config = config
        self.repository = repository
        self.leetcode = leetcode
        scope = f"{config.leetcode_username.casefold()}|{config.link.spreadsheet_id}|{config.link.worksheet_id}"
        suffix = hashlib.sha256(scope.encode()).hexdigest()[:12]
        self.store = PendingStore(state_dir / f"pending_{suffix}.json")
        self.stop_event = threading.Event()
        self.messages: queue.SimpleQueue[str] = queue.SimpleQueue()
        self.thread: threading.Thread | None = None
        self.last_summary: tuple | None = None
        self.last_error: str | None = None

    def poll_once(self) -> None:
        snapshot = self.leetcode.fetch_snapshot()
        # Сохраняем обнаруженное до обращения к Google: при сбое таблицы события не потеряются.
        self.store.discover(snapshot.problems)
        numbers, slugs = recorded_tasks(self.repository.read_rows(), self.config)
        self.store.remove_recorded(numbers, slugs)
        summary = (snapshot.total_solved, len(snapshot.problems), snapshot.complete, len(self.store.pending()))
        if summary != self.last_summary:
            self.messages.put(f"LeetCode: {snapshot.total_solved} решено; доступно для сверки: {len(snapshot.problems)}; ожидают difficulties: {summary[3]}.")
            if not snapshot.complete:
                self.messages.put("Открытая история неполная. Для сверки всех решённых задач один раз выполните: python bot.py --login. Последние решения отслеживаются и без входа.")
            self.last_summary = summary
        if self.last_error:
            self.messages.put("Подключение восстановлено, сверка продолжена.")
        self.last_error = None

    def _run(self) -> None:
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                self.poll_once()
            except (BotError, OSError, requests.RequestException) as error:
                message = str(error) if isinstance(error, (BotError, OSError)) else "Нет соединения с Google Таблицей."
                if message != self.last_error:
                    self.messages.put(message)
                    self.last_error = message
            if self.stop_event.wait(max(0, POLL_INTERVAL - (time.monotonic() - started))):
                break

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name="leetcode-sync", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=1)

    def already_recorded(self, problem: Problem) -> bool:
        numbers, slugs = recorded_tasks(self.repository.read_rows(), self.config)
        return problem.number in numbers or problem.slug in slugs


def run_sync(ui, engine: SyncEngine) -> int:
    deferred: set[str] = set()
    retry_at: dict[str, float] = {}
    ui.console.print("[dim]Проверка при запуске и затем раз в минуту, в том числе во время ввода заметок. Выход — Ctrl+C.[/dim]")
    engine.start()
    try:
        with ui.console.status("[cyan]Сверяю задачи и ожидаю новые решения…[/cyan]") as status:
            while not engine.stop_event.is_set():
                while not engine.messages.empty():
                    ui.console.print(engine.messages.get(), markup=False)
                item = next((item for item in engine.store.pending() if item.problem.slug not in deferred and time.monotonic() >= retry_at.get(item.problem.slug, 0)), None)
                if item is None:
                    engine.stop_event.wait(0.5)
                    continue
                status.stop()
                problem = item.problem
                try:
                    if engine.already_recorded(problem):
                        engine.store.acknowledge(problem.slug)
                        continue
                    problem = engine.leetcode.resolve(problem)
                    if engine.already_recorded(problem):
                        engine.store.acknowledge(problem.slug)
                        continue
                    ui.problem(problem)
                    notes = item.notes
                    resolve = item.resolve
                    if notes is None:
                        notes = ui.notes()
                        if notes is None:
                            deferred.add(problem.slug)
                            ui.warning("Задача отложена до следующего запуска.")
                            continue
                        engine.store.save(problem, notes)
                    else:
                        ui.console.print("[dim]Заметка восстановлена из локальной очереди.[/dim]")
                    if resolve is None:
                        resolve = ui.resolve_choice()
                        engine.store.save(problem, notes, resolve)
                    if engine.already_recorded(problem):
                        write_json(engine.store.path.with_name("last_difficulties.json"), {"problem": asdict(problem), "notes": notes, "resolve": resolve})
                        engine.store.acknowledge(problem.slug)
                        ui.warning("Задача уже появилась в таблице. Введённый текст сохранён локально в last_difficulties.json.")
                        continue
                    engine.repository.append(problem, notes, resolve)
                    engine.store.acknowledge(problem.slug)
                    ui.success(f"№{problem.number} — {problem.title}: difficulties и Resolve ({resolve}) сохранены.")
                except (BotError, OSError, requests.RequestException) as error:
                    ui.warning(str(error) if isinstance(error, (BotError, OSError)) else "Ошибка соединения с таблицей.")
                    ui.warning("Задача и завершённая заметка остаются в очереди. Проверю снова через минуту.")
                    retry_at[problem.slug] = time.monotonic() + POLL_INTERVAL
                finally:
                    status.start()
    finally:
        engine.stop()
    return 0
