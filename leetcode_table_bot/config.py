"""Локальная конфигурация и атомарное сохранение файлов."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

FIELDS = ("number", "title", "notes", "url")
DEFAULT_HEADERS = ["problem number", "problem name", "my difficulties", "link"]
DEFAULT_COLUMNS = dict(zip(FIELDS, range(1, 5)))


class BotError(Exception):
    """Ошибка, которую можно показать пользователю без traceback."""


@dataclass(frozen=True)
class SpreadsheetLink:
    spreadsheet_id: str
    worksheet_id: int | None


def parse_spreadsheet_url(value: str) -> SpreadsheetLink:
    parts = urlsplit(value.strip())
    match = re.fullmatch(r"/spreadsheets/(?:u/\d+/)?d/([\w-]+)(?:/.*)?", parts.path)
    if parts.scheme != "https" or parts.netloc != "docs.google.com" or not match:
        raise BotError("Нужна ссылка вида https://docs.google.com/spreadsheets/d/…/edit#gid=0")
    params = {**parse_qs(parts.query), **parse_qs(parts.fragment)}
    gid = params.get("gid", [None])[0]
    if gid is not None and not re.fullmatch(r"[0-9]+", gid):
        raise BotError("В ссылке некорректный gid листа: ожидается целое число.")
    return SpreadsheetLink(match.group(1), int(gid) if gid is not None else None)


def validate_columns(columns: object) -> dict[str, int]:
    if not isinstance(columns, dict) or set(columns) != set(FIELDS):
        raise BotError("В columns нужны поля number, title, notes и url.")
    if any(type(value) is not int or not 1 <= value <= 18278 for value in columns.values()):
        raise BotError("Номера столбцов должны быть целыми числами от 1 до 18278 (A–ZZZ).")
    if len(set(columns.values())) != len(FIELDS):
        raise BotError("Каждому полю нужен отдельный столбец.")
    return dict(columns)


def column_letter(index: int) -> str:
    result = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def column_number(value: str) -> int:
    value = value.strip().upper()
    if not re.fullmatch(r"[A-Z]{1,3}", value):
        raise BotError("Введите букву столбца: например A, B или AA.")
    result = 0
    for char in value:
        result = result * 26 + ord(char) - 64
    return result


def validate_script_url(value: str) -> str:
    if not isinstance(value, str):
        raise BotError("Нужна ссылка веб-приложения Apps Script, заканчивающаяся на /exec.")
    value = value.strip()
    parts = urlsplit(value)
    if (parts.scheme != "https" or parts.netloc != "script.google.com"
            or not re.fullmatch(r"/macros/s/[A-Za-z0-9_-]+/exec", parts.path)
            or parts.query or parts.fragment):
        raise BotError("Скопируйте Web app URL из развёртывания Apps Script: https://script.google.com/macros/s/…/exec")
    return value


@dataclass
class Config:
    spreadsheet_url: str
    credentials_file: str = "credentials.json"
    header_row: int = 1
    columns: dict[str, int] = field(default_factory=dict)
    # Старые конфиги без backend продолжают работать через Google API.
    backend: str = "google"
    script_url: str = ""
    script_token: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        parse_spreadsheet_url(self.spreadsheet_url)
        if not isinstance(self.credentials_file, str) or not self.credentials_file.strip():
            raise BotError("credentials_file должен содержать путь к JSON-ключу Google.")
        if type(self.header_row) is not int or not 1 <= self.header_row <= 1000000:
            raise BotError("header_row должен быть номером строки от 1 до 1000000.")
        # Пустая карта означает, что настройка столбцов ещё не завершена.
        if self.columns != {}:
            self.columns = validate_columns(self.columns)
        if self.backend not in {"google", "apps_script"}:
            raise BotError("backend должен быть google или apps_script.")
        if self.backend == "apps_script":
            self.script_url = validate_script_url(self.script_url)
            if not isinstance(self.script_token, str) or not re.fullmatch(r"[a-f0-9]{64}", self.script_token):
                raise BotError(
                    "Нужен токен из журнала выполнения функции setup: 64 символа (цифры и буквы a–f). "
                    "ID развёртывания и ссылка /exec не подходят. "
                    "Для повторного ввода запустите: python bot.py --apps-script"
                )

    @property
    def link(self) -> SpreadsheetLink:
        return parse_spreadsheet_url(self.spreadsheet_url)

    @classmethod
    def load(cls, path: Path) -> Config:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("ожидается JSON-объект")
            return cls(**data)
        except (ValueError, TypeError, OSError) as error:
            raise BotError(f"Не удалось прочитать {path}: {error}. Исправьте файл или запустите --setup.") from error

    def save(self, path: Path) -> None:
        write_json(path, asdict(self))


def write_json(path: Path, data: object) -> None:
    """Заменяет файл атомарно; временный файл доступен только владельцу."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(data, output, ensure_ascii=False, indent=2)
            output.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
