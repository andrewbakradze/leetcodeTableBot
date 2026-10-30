"""Интерактивный цикл приложения."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from .config import BotError, Config, FIELDS, column_letter, column_number, parse_spreadsheet_url, validate_columns, validate_script_url
from .leetcode import LeetCodeClient, Problem, parse_problem_number, parse_problem_url
from .sheets import SheetRepository, connect, detect_columns

if TYPE_CHECKING:
    from .ui import TerminalUI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Дневник решённых задач LeetCode в Google Таблицах.")
    parser.add_argument("--config", type=Path, default=Path("config.json"), help="локальный конфиг (по умолчанию ./config.json)")
    parser.add_argument("--setup", action="store_true", help="заново настроить таблицу и столбцы")
    parser.add_argument("--demo", action="store_true", help="показать интерфейс без Google и сети")
    connection = parser.add_mutually_exclusive_group()
    connection.add_argument("--apps-script", action="store_true", help="подключить таблицу через Apps Script, без JSON-ключа")
    connection.add_argument("--google-api", action="store_true", help="подключить таблицу через JSON-ключ Google")
    return parser


def first_setup(ui: TerminalUI, path: Path, *, google_api: bool = False) -> Config:
    ui.console.print("[bold]Подключим вашу таблицу.[/bold]")
    previous = None
    saved_url = None
    if path.exists():
        try:
            previous = Config.load(path)
            saved_url = previous.spreadsheet_url
        except BotError:
            # Неверный токен не должен заставлять пользователя заново искать ссылку на таблицу.
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("spreadsheet_url"), str):
                    parse_spreadsheet_url(data["spreadsheet_url"])
                    saved_url = data["spreadsheet_url"]
            except (OSError, ValueError, BotError):
                pass
    while True:
        url = ui.ask("Ссылка на Google Таблицу", default=saved_url)
        try:
            parse_spreadsheet_url(url)
            break
        except BotError as error:
            ui.warning(str(error))
    if google_api:
        ui.console.print("[muted]Путь к JSON-ключу Google считается от папки конфига. Инструкция — README.md.[/muted]")
        credentials_file = ui.ask("Путь к JSON-ключу Google", default="credentials.json")
        config = Config(spreadsheet_url=url, credentials_file=credentials_file)
    else:
        ui.console.print(
            "[muted]Подключение без JSON-ключа. Вставьте apps-script/Code.gs в Расширения → Apps Script.\n"
            "Запустите setup, разверните веб-приложение и скопируйте ссылку /exec и токен.\n"
            "Пошаговая инструкция: apps-script/SETUP.md.[/muted]"
        )
        while True:
            try:
                script_url = validate_script_url(ui.ask("Ссылка Web app URL (…/exec)"))
                break
            except BotError as error:
                ui.warning(str(error))
        while True:
            try:
                token = ui.ask_secret("Токен из журнала setup: 64 символа, не ID развёртывания (ввод скрыт)")
                config = Config(url, backend="apps_script", script_url=script_url, script_token=token)
                break
            except BotError as error:
                ui.warning(str(error))
    if previous and previous.spreadsheet_url == url:
        config.header_row = previous.header_row
        config.columns = previous.columns
    config.save(path)
    ui.success(f"Настройки сохранены: {path}")
    return config


def configure_columns(ui: TerminalUI, repository: SheetRepository, config_path: Path) -> None:
    config = repository.config
    rows = repository.read_rows()
    if not any(any(str(cell).strip() for cell in row) for row in rows):
        repository.initialize_empty_sheet()
        config.save(config_path)
        ui.success("В пустом листе созданы четыре заголовка.")
        return
    # В шаблоне пользователя заголовки в первой строке. Поддерживаем и таблицы с шапкой выше.
    candidates = [(index, detect_columns(row)) for index, row in enumerate(rows[:20], start=1)]
    header_row, detected = max(candidates, key=lambda pair: len(pair[1]))
    if len(detected) == len(FIELDS):
        config.header_row = header_row
        config.columns = validate_columns(detected)
        ui.success(f"Столбцы распознаны автоматически, заголовки в строке {header_row}.")
    else:
        ui.warning("Заголовки распознаны не полностью. Укажите расположение полей.")
        while True:
            try:
                row = int(ui.ask("Номер строки с заголовками", default=str(header_row)))
                if not 1 <= row <= len(rows):
                    raise ValueError
                config.header_row = row
                break
            except ValueError:
                ui.warning(f"Введите номер строки от 1 до {len(rows)}.")
        headers = rows[config.header_row - 1]
        for index, header in enumerate(headers, start=1):
            ui.console.print(f"  {column_letter(index)}: {header}", markup=False)
        detected = detect_columns(headers)
        labels = {"number": "Номер задачи", "title": "Название задачи", "notes": "Мои сложности", "url": "Ссылка"}
        while True:
            columns = {}
            try:
                for position, field in enumerate(FIELDS, start=1):
                    columns[field] = column_number(ui.ask(labels[field], default=column_letter(detected.get(field, position))))
                config.columns = validate_columns(columns)
                if max(columns.values()) > repository.worksheet.col_count:
                    raise BotError("В листе нет такого столбца.")
                break
            except BotError as error:
                ui.warning(str(error))
    ui.mapping(config, rows[config.header_row - 1])
    config.save(config_path)


def friendly_error(error: Exception) -> str:
    # Функция вызывается после проверки установленных зависимостей.
    import gspread
    import requests
    from google.auth.exceptions import GoogleAuthError

    if isinstance(error, BotError):
        return str(error)
    if isinstance(error, gspread.exceptions.SpreadsheetNotFound):
        return "Google Таблица не найдена. Проверьте ссылку и доступ аккаунта из JSON-ключа (нужна роль «Редактор»)."
    if isinstance(error, gspread.exceptions.WorksheetNotFound):
        return "Лист не найден. Проверьте gid в ссылке или запустите --setup."
    if isinstance(error, PermissionError) and isinstance(error.__cause__, gspread.exceptions.APIError):
        return "Google отказал в доступе. Дайте используемому аккаунту роль «Редактор» и включите Google Sheets API."
    if isinstance(error, gspread.exceptions.APIError):
        code = error.response.status_code
        if code in (401, 403):
            return "Google отказал в записи. Проверьте роль «Редактор», Google Sheets API и ограничения защищённых диапазонов."
        if code == 429:
            return "Превышен лимит Google Sheets API. Подождите минуту и повторите попытку."
        return f"Ошибка Google Sheets API (HTTP {code}). {error}"
    if isinstance(error, GoogleAuthError):
        return "Не удалось авторизоваться в Google. Проверьте JSON-ключ. Для повторного OAuth-входа удалите локальный authorized_user.json."
    if isinstance(error, requests.RequestException):
        return "Нет ответа от Google. Проверьте подключение к интернету и повторите попытку."
    return str(error)


def manual_problem(ui: TerminalUI, number: int) -> Problem | None:
    choice = ui.ask("Ввести название и ссылку вручную?", choices=["да", "отмена"], default="да")
    if choice == "отмена":
        return None
    while True:
        value = ui.ask("Ссылка на задачу LeetCode (/cancel — отменить)")
        if value.lower() == "/cancel":
            return None
        try:
            slug = parse_problem_url(value)
            break
        except BotError as error:
            ui.warning(str(error))
    while True:
        title = ui.ask("Название задачи (/cancel — отменить)")
        if title.lower() == "/cancel":
            return None
        if title and len(title) <= 50000:
            return Problem(number, title, slug)
        ui.warning("Введите название задачи (до 50 000 символов).")


def run(ui: TerminalUI, args: argparse.Namespace, error_types: tuple[type[Exception], ...]) -> int:
    config_path = args.config.expanduser().resolve()
    state_dir = config_path.parent / ".leetcode-table-bot"
    google_api = getattr(args, "google_api", False)
    reconfigure = args.setup or getattr(args, "apps_script", False) or google_api or not config_path.exists()
    config = first_setup(ui, config_path, google_api=google_api) if reconfigure else Config.load(config_path)
    ui.console.print("[muted]Подключение к вашей таблице…[/muted]")
    repository = connect(config, config_path, state_dir)
    if not config.columns:
        configure_columns(ui, repository, config_path)
    ui.success(f"Подключён лист «{repository.worksheet.title}».")
    ui.help()
    leetcode = LeetCodeClient(state_dir / "leetcode_cache.json")
    draft_path = state_dir / "last_entry.json"
    while True:
        value = ui.ask("Номер задачи")
        command = value.lower()
        if command in {"/exit", "exit", "q", "/q", "выход", "/выход"}:
            return 0
        if not value:
            continue
        if command in {"/help", "help", "?"}:
            ui.help()
            continue
        if command == "/config":
            ui.console.print(f"Конфиг: {config_path}\nТаблица: {config.spreadsheet_url}", markup=False)
            ui.mapping(config, repository.worksheet.row_values(config.header_row))
            continue
        if command == "/draft":
            try:
                data = json.loads(draft_path.read_text(encoding="utf-8"))
                ui.show_draft(Problem(**data["problem"]), data["notes"])
            except (OSError, ValueError, KeyError, TypeError):
                ui.warning("Локальной копии пока нет.")
            continue
        try:
            number = parse_problem_number(value)
            try:
                with ui.console.status("[cyan]Получаю данные задачи…[/cyan]"):
                    problem = leetcode.get_problem(number)
                if leetcode.warning:
                    ui.warning(leetcode.warning)
            except BotError as error:
                ui.warning(str(error))
                problem = manual_problem(ui, number)
                if problem is None:
                    continue
            ui.problem(problem)
            with ui.console.status("[cyan]Проверяю журнал…[/cyan]"):
                existing = repository.find_entries(number)
            action = "новая"
            if existing:
                ui.warning(f"Задача уже есть в строке {existing[-1].row}.")
                action = ui.ask("Как сохранить?", choices=["дополнить", "новая", "отмена"], default="дополнить")
                if action == "отмена":
                    continue
            notes = ui.notes()
            if notes is None:
                ui.warning("Добавление задачи отменено.")
                continue
            if action == "дополнить" and not notes:
                ui.warning("Новая заметка пустая. Запись уже есть в таблице.")
                continue
            # Копия создаётся до обращения к Google: заметка останется при ошибке сети.
            LeetCodeClient.backup(draft_path, problem, notes)
            try:
                with ui.console.status("[cyan]Сохраняю в Google Таблицу…[/cyan]"):
                    if action == "дополнить":
                        repository.add_notes(existing[-1], notes)
                    else:
                        repository.append(problem, notes)
            except error_types:
                ui.warning(f"Локальная копия: {draft_path} (команда /draft). Перед повтором проверьте таблицу.")
                raise
            ui.success(f"Задача №{number} сохранена." if action == "новая" else f"Заметка к задаче №{number} дополнена.")
            ui.console.print()
        except error_types as error:
            ui.error(friendly_error(error))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        from .ui import TerminalUI
    except ModuleNotFoundError as error:
        print(f"Не установлена библиотека {error.name}. Выполните: python -m pip install -r requirements.txt", file=sys.stderr)
        return 1
    ui = TerminalUI()
    if args.demo:
        ui.demo()
        return 0
    try:
        import gspread
        import requests
        from google.auth.exceptions import GoogleAuthError
    except ModuleNotFoundError as error:
        ui.error(f"Не установлена библиотека {error.name}. Выполните: python -m pip install -r requirements.txt")
        return 1
    error_types = (BotError, gspread.exceptions.GSpreadException, GoogleAuthError, requests.RequestException, OSError, ValueError)
    ui.banner()
    try:
        return run(ui, args, error_types)
    except (KeyboardInterrupt, EOFError):
        ui.console.print("\n[muted]До следующей задачи![/muted]")
        return 0
    except error_types as error:
        ui.error(friendly_error(error))
        return 1
