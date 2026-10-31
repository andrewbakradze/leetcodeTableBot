"""Запуск автоматической сверки LeetCode и Google Таблицы."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import BotError, Config, FIELDS, column_letter, column_number, parse_spreadsheet_url, validate_columns, validate_script_url
from .leetcode import LeetCodeClient, parse_username
from .sheets import SheetRepository, connect, detect_columns
from .sync import SyncEngine, run_sync


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Автоматическая сверка решённых задач LeetCode с Google Таблицей раз в минуту.")
    parser.add_argument("--config", type=Path, default=Path("config.json"), help="путь к локальному конфигу")
    parser.add_argument("--setup", action="store_true", help="изменить таблицу, Apps Script или профиль LeetCode")
    parser.add_argument("--login", action="store_true", help="локально подключить сессию LeetCode для сверки всей истории")
    return parser


def first_setup(ui, path: Path) -> Config:
    saved: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                saved = data
        except (OSError, ValueError):
            pass
    ui.console.print("[bold]Подключение таблицы и профиля LeetCode[/bold]")
    while True:
        url = ui.ask("Ссылка на Google Таблицу", default=saved.get("spreadsheet_url"))
        try:
            parse_spreadsheet_url(url)
            break
        except BotError as error:
            ui.warning(str(error))
    while True:
        try:
            username = parse_username(ui.ask("Профиль LeetCode или username", default=saved.get("leetcode_username", "1-andro1d-1")))
            break
        except BotError as error:
            ui.warning(str(error))
    ui.console.print("[dim]Подключение Google — через ваш Apps Script. Инструкция: apps-script/SETUP.md.[/dim]")
    while True:
        try:
            script_url = validate_script_url(ui.ask("Web app URL (…/exec)", default=saved.get("script_url")))
            break
        except BotError as error:
            ui.warning(str(error))
    while True:
        try:
            token = ui.ask_secret("Токен из журнала setup: 64 символа (ввод скрыт)")
            config = Config(url, script_url=script_url, script_token=token, leetcode_username=username)
            break
        except BotError as error:
            ui.warning(str(error))
    config.save(path)
    ui.success("Настройки сохранены.")
    return config


def configure_columns(ui, repository: SheetRepository, config_path: Path) -> None:
    config = repository.config
    rows = repository.read_rows()
    if not any(any(cell.strip() for cell in row) for row in rows):
        repository.initialize_empty_sheet()
        config.save(config_path)
        return
    header_row, detected = max(((index, detect_columns(row)) for index, row in enumerate(rows[:20], 1)), key=lambda pair: len(pair[1]))
    if len(detected) == len(FIELDS):
        config.header_row = header_row
        config.columns = validate_columns(detected)
    else:
        ui.warning("Не все заголовки распознаны. Укажите строку заголовков и столбцы один раз.")
        while True:
            try:
                row = int(ui.ask("Строка заголовков", default=str(header_row)))
                if not 1 <= row <= len(rows):
                    raise ValueError
                config.header_row = row
                break
            except ValueError:
                ui.warning(f"Нужен номер строки от 1 до {len(rows)}.")
        headers = rows[config.header_row - 1]
        for index, header in enumerate(headers, 1):
            ui.console.print(f"{column_letter(index)}: {header}", markup=False)
        labels = {"number": "Номер задачи", "title": "Название", "notes": "Difficulties", "url": "Ссылка", "resolve": "Resolve"}
        while True:
            try:
                columns = {field: column_number(ui.ask(labels[field], default=column_letter(index))) for index, field in enumerate(FIELDS, 1)}
                config.columns = validate_columns(columns)
                if max(columns.values()) > repository.worksheet.col_count:
                    raise BotError("В листе нет такого столбца.")
                break
            except BotError as error:
                ui.warning(str(error))
    config.save(config_path)
    ui.success("Столбцы таблицы настроены.")


def run(ui, args: argparse.Namespace) -> int:
    config_path = args.config.expanduser().resolve()
    config = first_setup(ui, config_path) if args.setup or not config_path.exists() else Config.load(config_path)
    config.save(config_path)  # Сохраняет миграцию предыдущего конфига и профиль пользователя.
    state_dir = config_path.parent / ".leetcode-table-bot"
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_dir.chmod(0o700)
    leetcode = LeetCodeClient(config.leetcode_username, state_dir, load_session=not args.login)
    if args.login:
        ui.console.print(
            "[bold]Подключение своей сессии LeetCode для всей истории[/bold]\n"
            "[dim]Войдите на leetcode.com → F12 / Инструменты разработчика → Application → Cookies → https://leetcode.com.\n"
            "Скопируйте значения LEETCODE_SESSION и csrftoken только в этот терминал.\n"
            "Ввод скрыт. Данные сохраняются локально и используются только для чтения LeetCode.[/dim]"
        )
        session = ui.ask_secret("Значение LEETCODE_SESSION")
        csrf = ui.ask_secret("Значение csrftoken")
        with ui.console.status("[cyan]Проверяю аккаунт LeetCode…[/cyan]"):
            leetcode.login(session, csrf)
        ui.success("Сессия нужного профиля подключена.")
    with ui.console.status("[cyan]Подключаю Google Таблицу…[/cyan]"):
        repository = connect(config)
    configure_columns(ui, repository, config_path)
    ui.success(f"Лист «{repository.worksheet.title}»; профиль LeetCode: {config.leetcode_username}.")
    return run_sync(ui, SyncEngine(config, repository, leetcode, state_dir))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        from .ui import TerminalUI
    except ModuleNotFoundError as error:
        print(f"Не установлена библиотека {error.name}. Выполните: python -m pip install -r requirements.txt", file=sys.stderr)
        return 1
    ui = TerminalUI()
    try:
        # Rich включает альтернативный экран только в терминале и восстанавливает
        # обычный экран при любом выходе, в том числе Ctrl+C и исключениях.
        with ui.console.screen(hide_cursor=False):
            ui.banner()
            return run(ui, args)
    except (KeyboardInterrupt, EOFError):
        ui.console.print("\n[dim]Остановлено. Очередь и завершённые заметки сохранены.[/dim]")
        return 0
    except (BotError, OSError, ValueError) as error:
        ui.error(str(error))
        return 1
