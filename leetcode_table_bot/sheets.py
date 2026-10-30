"""Авторизация Google и запись в существующий лист без изменения чужих ячеек."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import BotError, Config, DEFAULT_COLUMNS, DEFAULT_HEADERS, column_letter
from .leetcode import Problem

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
ALIASES = {
    "number": {"problemnumber", "number", "id", "номер", "номерзадачи", "№", "#"},
    "title": {"problemname", "problemtitle", "title", "name", "название", "названиезадачи", "задача"},
    "notes": {"mydifficulties", "difficulties", "notes", "сложности", "моисложности", "заметки", "комментарий"},
    "url": {"link", "url", "problemlink", "ссылка", "ссылканазадачу", "leetcode"},
}


def detect_columns(headers: list[str]) -> dict[str, int]:
    columns = {}
    for index, header in enumerate(headers, start=1):
        normalized = re.sub(r"[\s_.\-:]+", "", header.strip().lower())
        for field, aliases in ALIASES.items():
            if normalized in aliases and field not in columns:
                columns[field] = index
    return columns


def connect(config: Config, config_path: Path, state_dir: Path) -> SheetRepository:
    if config.backend == "apps_script":
        from .apps_script import ScriptRepository

        return ScriptRepository(config)
    # Импорт здесь позволяет проверять логику без сетевых зависимостей.
    import gspread

    path = Path(config.credentials_file).expanduser()
    if not path.is_absolute():
        path = config_path.parent / path
    try:
        credentials = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise BotError(
            f"Не удалось прочитать ключ Google: {path}. "
            "Скачайте JSON-ключ, как описано в README.md, и укажите credentials_file в config.json."
        ) from error
    if not isinstance(credentials, dict):
        raise BotError("JSON-ключ Google должен содержать объект.")
    if credentials.get("type") == "service_account":
        client = gspread.service_account_from_dict(credentials, scopes=SCOPES)
    elif "installed" in credentials:
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        token = state_dir / "authorized_user.json"
        # gspread сохраняет OAuth-токен самостоятельно. Каталог закрыт для других пользователей.
        state_dir.chmod(0o700)
        try:
            client = gspread.oauth(
                scopes=SCOPES,
                credentials_filename=str(path),
                authorized_user_filename=str(token),
            )
        finally:
            if token.exists():
                token.chmod(0o600)
    else:
        raise BotError("Нужен JSON для OAuth «Desktop app» или сервисного аккаунта Google.")
    client.set_timeout((10, 30))
    spreadsheet = client.open_by_key(config.link.spreadsheet_id)
    if config.link.worksheet_id is None:
        worksheet = spreadsheet.get_worksheet(0)
    else:
        worksheet = spreadsheet.get_worksheet_by_id(config.link.worksheet_id)
    if worksheet is None:
        raise BotError("Лист не найден. Проверьте gid в ссылке Google Таблицы.")
    return SheetRepository(worksheet, config)


@dataclass(frozen=True)
class ExistingEntry:
    row: int
    notes: str
    number: int


def matches_number(value: str, number: int) -> bool:
    value = value.strip()
    return bool(re.fullmatch(r"[0-9]+(?:[.,]0+)?", value)) and int(value.split(".")[0].split(",")[0]) == number


class SheetRepository:
    def __init__(self, worksheet: Any, config: Config) -> None:
        self.worksheet = worksheet
        self.config = config

    def read_rows(self) -> list[list[str]]:
        return self.worksheet.get_all_values()

    def initialize_empty_sheet(self) -> None:
        # Проверяется весь лист, а не только первая строка.
        if any(any(cell.strip() for cell in row) for row in self.read_rows()):
            raise BotError("В листе уже есть данные. Сначала настройте столбцы.")
        if self.worksheet.col_count < 4:
            self.worksheet.add_cols(4 - self.worksheet.col_count)
        self.worksheet.update(range_name="A1:D1", values=[DEFAULT_HEADERS], value_input_option="RAW")
        self.config.header_row = 1
        self.config.columns = dict(DEFAULT_COLUMNS)

    def find_entries(self, number: int) -> list[ExistingEntry]:
        rows = self.read_rows()
        number_index = self.config.columns["number"] - 1
        notes_index = self.config.columns["notes"] - 1
        entries = []
        for row_number, row in enumerate(rows[self.config.header_row:], self.config.header_row + 1):
            value = row[number_index].strip() if number_index < len(row) else ""
            # Google может отформатировать числовую ячейку как 228.0 / 228,0.
            if matches_number(value, number):
                entries.append(ExistingEntry(row_number, row[notes_index] if notes_index < len(row) else "", number))
        return entries

    def append(self, problem: Problem, notes: str) -> None:
        columns = self.config.columns
        width = max(columns.values())
        if self.worksheet.col_count < width:
            raise BotError("Настроенные столбцы выходят за границы листа. Запустите --setup.")
        row: list[str | int] = [""] * width
        for field, value in {"number": problem.number, "title": problem.title, "notes": notes, "url": problem.url}.items():
            row[columns[field] - 1] = value
        # RAW сохраняет заметки буквально, в том числе начинающиеся с '='.
        self.worksheet.append_row(
            row,
            value_input_option="RAW",
            insert_data_option="INSERT_ROWS",
            table_range=f"A{self.config.header_row}:{column_letter(width)}",
        )

    def add_notes(self, entry: ExistingEntry, notes: str) -> None:
        # Строку могли переместить или дополнить вручную во время ввода заметки.
        row = self.worksheet.row_values(entry.row)
        number_index = self.config.columns["number"] - 1
        notes_index = self.config.columns["notes"] - 1
        if number_index >= len(row) or not matches_number(row[number_index], entry.number):
            raise BotError("Строка задачи изменилась за время ввода. Проверьте таблицу и введите номер заново; заметка есть в /draft.")
        cell_name = f"{column_letter(self.config.columns['notes'])}{entry.row}"
        current = row[notes_index] if notes_index < len(row) else ""
        combined = f"{current}\n\n{notes}" if current else notes
        if len(combined) > 50000:
            raise BotError("Заметка вместе с предыдущей превышает лимит Google: 50 000 символов.")
        self.worksheet.update(range_name=cell_name, values=[[combined]], value_input_option="RAW")
