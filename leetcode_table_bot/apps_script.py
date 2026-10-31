"""Подключение к Google Таблице через свой Apps Script, без JSON-ключа."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any

import requests

from .config import BotError, Config, DEFAULT_COLUMNS, RESOLVE_OPTIONS
from .leetcode import Problem


class ScriptClient:
    def __init__(self, config: Config) -> None:
        self.config = config

    def _request(self, action: str, payload: dict[str, Any]) -> requests.Response:
        # Повторять можно только чтение: после потери ответа запись уже могла выполниться.
        attempts = 3 if action in ("metadata", "read") else 1
        for attempt in range(attempts):
            try:
                response = requests.post(self.config.script_url, json=payload, timeout=(10, 30))
                response.raise_for_status()
                return response
            except requests.exceptions.SSLError:
                raise
            except (requests.Timeout, requests.ConnectionError):
                if attempt + 1 == attempts:
                    raise
            except requests.HTTPError as error:
                status = error.response.status_code if error.response is not None else None
                if status not in (502, 503, 504) or attempt + 1 == attempts:
                    raise
            time.sleep(2 ** (attempt + 1))
        raise AssertionError("Нет попыток запроса Apps Script")

    def call(self, action: str, **fields: Any) -> dict[str, Any]:
        config = self.config
        payload = {
            **fields,
            "action": action,
            "token": config.script_token,
            "spreadsheet_id": config.link.spreadsheet_id,
            "worksheet_id": config.link.worksheet_id,
            "columns": config.columns,
            "header_row": config.header_row,
        }
        try:
            # Токен передаётся только в теле HTTPS-запроса, не в URL.
            # Apps Script возвращает результат через перенаправление на googleusercontent.com.
            response = self._request(action, payload)
        except requests.exceptions.SSLError as error:
            raise BotError("Не удалось проверить HTTPS-сертификат Google. Проверьте дату компьютера и доверенные сертификаты Python.") from error
        except requests.Timeout as error:
            if action in ("metadata", "read"):
                raise BotError(
                    "Не удалось получить ответ Apps Script после трёх попыток. "
                    "Проверьте соединение, VPN или прокси и доступ к script.google.com и script.googleusercontent.com. "
                    "Если doPost в Apps Script завершён, задержка могла возникнуть при доставке ответа."
                ) from error
            raise BotError(
                "Не удалось дождаться ответа Apps Script на запись. Google мог уже сохранить данные. "
                "Перед повторной отправкой бот перечитает таблицу."
            ) from error
        except requests.HTTPError as error:
            status = error.response.status_code if error.response is not None else None
            if status in (401, 403):
                raise BotError(
                    f"Google требует вход или не разрешает доступ к веб-приложению (HTTP {status}). "
                    "В Apps Script откройте «Начать развёртывание → Управление развёртываниями → Изменить». "
                    "Укажите «Выполнять от имени: Я» и «У кого есть доступ: Все / Anyone» "
                    "(доступ без входа в Google), затем нажмите «Развернуть»."
                ) from error
            if status == 404:
                raise BotError("Развёртывание Apps Script не найдено (HTTP 404). Скопируйте актуальный Web app URL с окончанием /exec и запустите --setup.") from error
            if status == 429:
                raise BotError("Google ограничил запросы к Apps Script (HTTP 429). Подождите и повторите попытку.") from error
            raise BotError(f"Ошибка веб-приложения Apps Script (HTTP {status or 'неизвестен'}). Проверьте развёртывание.") from error
        except requests.RequestException as error:
            raise BotError(
                "Не удалось установить соединение с Apps Script. Проверьте интернет, VPN или прокси."
            ) from error
        try:
            body = response.json()
        except ValueError as error:
            raise BotError("Apps Script вернул страницу вместо JSON. Используйте ссылку /exec и доступ Anyone (Все).") from error
        if not isinstance(body, dict):
            raise BotError("Apps Script вернул некорректный ответ. Обновите развёртывание скрипта.")
        if body.get("ok") is not True:
            message = body.get("error", "Неизвестная ошибка Apps Script.")
            raise BotError(message.replace("--apps-script", "--setup") if isinstance(message, str) else "Ошибка Apps Script.")
        data = body.get("data")
        if not isinstance(data, dict):
            raise BotError("В ответе Apps Script нет данных. Обновите развёртывание скрипта.")
        return data


@dataclass
class ScriptWorksheet:
    title: str
    col_count: int


class ScriptRepository:
    def __init__(self, config: Config) -> None:
        self.client = ScriptClient(config)
        metadata = self.client.call("metadata")
        if not isinstance(metadata.get("title"), str) or type(metadata.get("col_count")) is not int:
            raise BotError("Apps Script вернул некорректные данные листа.")
        if metadata.get("supports_resolve") is not True:
            raise BotError(
                "Развёрнут старый Apps Script без поля Resolve. Замените код на apps-script/Code.gs, "
                "затем: Начать развёртывание → Управление развёртываниями → Изменить → Новая версия → Развернуть. "
                "Ссылка и токен остаются прежними."
            )
        self.worksheet = ScriptWorksheet(metadata["title"], metadata["col_count"])
        self.config = config

    def read_rows(self) -> list[list[str]]:
        rows = self.client.call("read").get("rows")
        if not isinstance(rows, list) or any(not isinstance(row, list) or any(not isinstance(cell, str) for cell in row) for row in rows):
            raise BotError("Apps Script вернул некорректные строки таблицы.")
        return rows

    def initialize_empty_sheet(self) -> None:
        self.client.call("initialize")
        self.config.header_row = 1
        self.config.columns = dict(DEFAULT_COLUMNS)
        self.worksheet.col_count = max(len(DEFAULT_COLUMNS), self.worksheet.col_count)

    def append(self, problem: Problem, notes: str, resolve: str) -> None:
        if problem.number is None:
            raise BotError("У задачи ещё не определён номер.")
        if resolve not in RESOLVE_OPTIONS:
            raise BotError("Выберите Resolve: ДА, может быть или нет.")
        self.client.call("append", number=problem.number, title=problem.title, notes=notes, url=problem.url, resolve=resolve)
