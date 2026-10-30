"""Получение публичного каталога LeetCode без cookies и пароля."""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import Request, urlopen

from .config import BotError, write_json

CATALOG_URL = "https://leetcode.com/api/problems/all/"
CACHE_TTL = 24 * 60 * 60
DIFFICULTIES = {1: "Easy", 2: "Medium", 3: "Hard"}
MIRROR_ROOT = "https://raw.githubusercontent.com/doocs/leetcode/main"
MIRROR_INDEX_URL = MIRROR_ROOT + "/solution/README_EN.md"


@dataclass(frozen=True)
class Problem:
    number: int
    title: str
    slug: str
    difficulty: str = ""

    @property
    def url(self) -> str:
        return f"https://leetcode.com/problems/{self.slug}/"


def parse_problem_number(value: str) -> int:
    value = value.strip()
    if not re.fullmatch(r"[0-9]{1,7}", value) or int(value) < 1:
        raise BotError("Введите положительный номер задачи, например 1 или 228.")
    return int(value)


def parse_problem_url(value: str) -> str:
    """Возвращает slug из скопированной ссылки, отбрасывая вкладку и параметры."""
    parts = urlsplit(value.strip())
    match = re.fullmatch(r"/problems/([a-z0-9]+(?:-[a-z0-9]+)*)(?:/.*)?", parts.path)
    if parts.scheme != "https" or parts.netloc not in {"leetcode.com", "www.leetcode.com"} or not match:
        raise BotError("Вставьте ссылку вида https://leetcode.com/problems/two-sum/ или /cancel.")
    return match.group(1)


def parse_mirror_index(text: str) -> dict[int, tuple[str, str]]:
    """Читает только номер, путь к документу и сложность из открытого каталога doocs."""
    entries = {}
    for line in text.splitlines():
        match = re.match(r"^\|\s*([0-9]+)\s*\|\s*\[.+?\]\((/solution/[^)]+/README_EN\.md)\)\s*\|[^|]*\|\s*(Easy|Medium|Hard)\s*\|", line)
        if not match:
            continue
        number = int(match.group(1))
        path = unquote(match.group(2))
        # Запросы идут только к файлам одного репозитория; внешние URL и переходы ../ не принимаются.
        if re.fullmatch(r"/solution/[0-9]+-[0-9]+/0*" + str(number) + r"\.[^/?#]+/README_EN\.md", path):
            entries[number] = (quote(path, safe="/"), match.group(3))
    if not entries:
        raise BotError("Резервный каталог задач вернул неожиданный формат.")
    return entries


def parse_mirror_problem(text: str, number: int, difficulty: str) -> Problem:
    match = re.search(
        r"^#\s+\[([0-9]+)\.\s+(.+?)\]\(https://leetcode\.com/problems/([a-z0-9]+(?:-[a-z0-9]+)*)/?\)",
        text, re.MULTILINE,
    )
    if not match or int(match.group(1)) != number:
        raise BotError("В резервном источнике не удалось проверить номер и ссылку задачи.")
    return Problem(number, match.group(2), match.group(3), difficulty)


def parse_catalog(payload: object) -> dict[int, Problem]:
    if not isinstance(payload, dict) or not isinstance(payload.get("stat_status_pairs"), list):
        raise BotError("LeetCode вернул неожиданный ответ. Попробуйте позже.")
    problems = {}
    for item in payload["stat_status_pairs"]:
        try:
            stat = item["stat"]
            # question_id — внутренний ID; пользователь вводит frontend_question_id.
            number = parse_problem_number(str(stat["frontend_question_id"]))
            title = stat["question__title"]
            slug = stat["question__title_slug"]
            if not isinstance(title, str) or not title.strip():
                continue
            if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
                continue
            difficulty = DIFFICULTIES.get(item.get("difficulty", {}).get("level"), "")
            problems[number] = Problem(number, title, slug, difficulty)
        except (KeyError, TypeError, AttributeError, BotError):
            # В каталоге встречаются задачи с нечисловыми номерами (LCP, LCR и т. п.).
            continue
    if not problems:
        raise BotError("В ответе LeetCode не найдено задач с числовыми номерами.")
    return problems


class LeetCodeClient:
    def __init__(self, cache_file: Path) -> None:
        self.cache_file = cache_file
        self.problems: dict[int, Problem] = {}
        self.fetched_at = 0.0
        self.warning: str | None = None
        self._primary_error: BotError | None = None
        self._mirror_index: dict[int, tuple[str, str]] = {}
        self._read_cache()

    def _read_cache(self) -> None:
        try:
            cached = json.loads(self.cache_file.read_text(encoding="utf-8"))
            problems = parse_catalog(cached["catalog"])
            fetched_at = float(cached["fetched_at"])
            if not 0 < fetched_at <= time.time() + 60:
                return
            self.problems = problems
            self.fetched_at = fetched_at
        except (OSError, ValueError, TypeError, KeyError, BotError):
            pass  # Повреждённый кеш не мешает загрузить свежие данные.

    def _refresh(self) -> None:
        request = Request(CATALOG_URL, headers={
            "User-Agent": "LeetCodeTableBot/1.0 (+local personal study journal)",
            "Accept": "application/json",
            "Referer": "https://leetcode.com/problemset/",
        })
        try:
            with urlopen(request, timeout=25) as response:
                payload = json.load(response)
            self.problems = parse_catalog(payload)
            self.fetched_at = time.time()
        except HTTPError as error:
            if error.code in (403, 429):
                raise BotError(f"LeetCode ограничил запросы (HTTP {error.code}). Попробуйте позже.") from error
            raise BotError(f"Ошибка LeetCode: HTTP {error.code}.") from error
        except (URLError, TimeoutError, OSError) as error:
            raise BotError("Не удалось подключиться к LeetCode. Проверьте интернет.") from error
        except (ValueError, UnicodeError) as error:
            raise BotError("Вместо каталога LeetCode вернул некорректный ответ.") from error
        try:
            write_json(self.cache_file, {"fetched_at": self.fetched_at, "catalog": payload})
        except OSError:
            self.warning = "Не удалось сохранить кеш LeetCode; данные загружены из сети."

    def get_problem(self, number: int) -> Problem:
        self.warning = None
        stale = time.time() - self.fetched_at >= CACHE_TTL
        if stale or number not in self.problems:
            try:
                if self._primary_error is not None:
                    raise self._primary_error
                self._refresh()
            except BotError as error:
                self._primary_error = error
                if number not in self.problems:
                    try:
                        problem = self._get_mirror_problem(number)
                    except BotError:
                        raise BotError(f"{error} Резервный источник тоже недоступен или не содержит задачу. Можно ввести название и ссылку вручную.") from error
                    self.problems[number] = problem
                    self.fetched_at = time.time()
                    self._save_metadata()
                    self.warning = "LeetCode недоступен; название и ссылка получены из открытого каталога doocs/leetcode."
                    return problem
                self.warning = "LeetCode недоступен; данные задачи взяты из локального кеша."
        if number not in self.problems:
            raise BotError(f"Задача №{number} не найдена в каталоге LeetCode. Проверьте номер.")
        return self.problems[number]

    @staticmethod
    def _download_text(url: str) -> str:
        request = Request(url, headers={"User-Agent": "LeetCodeTableBot/1.0", "Accept": "text/plain"})
        try:
            with urlopen(request, timeout=20) as response:
                return response.read(5_000_000).decode("utf-8")
        except (HTTPError, URLError, OSError, ValueError, UnicodeError) as error:
            raise BotError("Резервный источник задач недоступен.") from error

    def _get_mirror_problem(self, number: int) -> Problem:
        if not self._mirror_index:
            index_file = self.cache_file.with_name("doocs_index.json")
            try:
                cached = json.loads(index_file.read_text(encoding="utf-8"))
                if not 0 <= time.time() - float(cached["fetched_at"]) < CACHE_TTL:
                    raise ValueError("stale")
                self._mirror_index = parse_mirror_index(cached["text"])
            except (OSError, ValueError, TypeError, KeyError, AttributeError, BotError):
                text = self._download_text(MIRROR_INDEX_URL)
                self._mirror_index = parse_mirror_index(text)
                try:
                    write_json(index_file, {"fetched_at": time.time(), "text": text, "source": MIRROR_INDEX_URL})
                except OSError:
                    pass
        if number not in self._mirror_index:
            raise BotError(f"В резервном каталоге нет задачи №{number}.")
        path, difficulty = self._mirror_index[number]
        # Ссылку берём из заголовка документа, а не угадываем по названию задачи.
        return parse_mirror_problem(self._download_text(MIRROR_ROOT + path), number, difficulty)

    def _save_metadata(self) -> None:
        levels = {value: key for key, value in DIFFICULTIES.items()}
        catalog = {"stat_status_pairs": [
            {"stat": {"frontend_question_id": problem.number, "question__title": problem.title,
                      "question__title_slug": problem.slug}, "difficulty": {"level": levels.get(problem.difficulty)}}
            for problem in self.problems.values()
        ]}
        try:
            write_json(self.cache_file, {"fetched_at": self.fetched_at, "catalog": catalog})
        except OSError:
            pass

    @staticmethod
    def backup(path: Path, problem: Problem, notes: str) -> None:
        write_json(path, {"problem": asdict(problem), "notes": notes})
