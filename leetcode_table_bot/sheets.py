"""Сопоставление полей и уже внесённых задач Google Таблицы."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Protocol, Any
from urllib.parse import urlsplit

from .config import Config
from .leetcode import Problem

if TYPE_CHECKING:
    from .apps_script import ScriptRepository

ALIASES = {
    "number": {"problemnumber", "number", "id", "номер", "номерзадачи", "№", "#"},
    "title": {"problemname", "problemtitle", "title", "name", "название", "названиезадачи", "задача"},
    "notes": {"mydifficulties", "difficulties", "notes", "сложности", "моисложности", "заметки", "комментарий"},
    "url": {"link", "url", "problemlink", "ссылка", "ссылканазадачу", "leetcode"},
    "resolve": {"resolve", "перерешать", "повторить", "решитьповторно"},
}


def detect_columns(headers: list[str]) -> dict[str, int]:
    columns = {}
    for index, header in enumerate(headers, start=1):
        normalized = re.sub(r"[\s_.\-:]+", "", header.strip().lower())
        for field, aliases in ALIASES.items():
            if normalized in aliases and field not in columns:
                columns[field] = index
    return columns


class SheetRepository(Protocol):
    config: Config
    worksheet: Any

    def read_rows(self) -> list[list[str]]: ...
    def initialize_empty_sheet(self) -> None: ...
    def append(self, problem: Problem, notes: str, resolve: str) -> None: ...


def connect(config: Config) -> ScriptRepository:
    from .apps_script import ScriptRepository

    return ScriptRepository(config)


def recorded_tasks(rows: list[list[str]], config: Config) -> tuple[set[int], set[str]]:
    numbers: set[int] = set()
    slugs: set[str] = set()
    number_index = config.columns["number"] - 1
    url_index = config.columns["url"] - 1
    for row in rows[config.header_row:]:
        number = row[number_index].strip() if number_index < len(row) else ""
        if re.fullmatch(r"[0-9]+(?:[.,]0+)?", number):
            numbers.add(int(number.split(".")[0].split(",")[0]))
        url = row[url_index].strip() if url_index < len(row) else ""
        try:
            parts = urlsplit(url)
        except ValueError:
            continue
        match = re.match(r"^/problems/([a-z0-9]+(?:-[a-z0-9]+)*)(?:/|$)", parts.path)
        if parts.netloc in {"leetcode.com", "www.leetcode.com"} and match:
            slugs.add(match.group(1))
    return numbers, slugs
