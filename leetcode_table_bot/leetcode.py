"""История принятых решений и полный список решённых задач своего аккаунта."""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

import requests

from .config import BotError, write_json

GRAPHQL_URL = "https://leetcode.com/graphql/"
RECENT_QUERY = """
query JournalProfile($username: String!, $limit: Int!) {
  matchedUser(username: $username) {
    username submitStatsGlobal { acSubmissionNum { difficulty count } }
  }
  recentAcSubmissionList(username: $username, limit: $limit) { title titleSlug timestamp }
}
"""
SOLVED_QUERY = """
query JournalSolved($skip: Int!, $limit: Int!, $filters: QuestionListFilterInput) {
  solved: questionList(categorySlug: "", skip: $skip, limit: $limit, filters: $filters) {
    total: totalNum
    questions: data { number: questionFrontendId title titleSlug difficulty status }
  }
}
"""
DETAIL_QUERY = """
query JournalQuestion($slug: String!) {
  question(titleSlug: $slug) { number: questionFrontendId title titleSlug difficulty }
}
"""


def parse_username(value: str) -> str:
    value = value.strip()
    if "://" in value:
        parts = urlsplit(value)
        match = re.fullmatch(r"/(?:u/)?([A-Za-z0-9_-]+)/?", parts.path)
        if parts.scheme != "https" or parts.netloc != "leetcode.com" or not match:
            raise BotError("Укажите username или ссылку https://leetcode.com/u/username/.")
        value = match.group(1)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
        raise BotError("В username допустимы латинские буквы, цифры, дефис и подчёркивание.")
    return value


@dataclass(frozen=True)
class Problem:
    number: int | None
    title: str
    slug: str
    difficulty: str = ""
    timestamp: int = 0

    @property
    def url(self) -> str:
        return f"https://leetcode.com/problems/{self.slug}/"

    @classmethod
    def from_data(cls, data: dict) -> Problem:
        if not isinstance(data, dict):
            raise BotError("LeetCode вернул некорректную задачу.")
        slug = data.get("titleSlug", data.get("slug"))
        title = data.get("title")
        if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
            raise BotError("LeetCode вернул некорректную ссылку задачи.")
        if not isinstance(title, str) or not title.strip() or len(title) > 50000:
            raise BotError("LeetCode вернул некорректное название задачи.")
        number = data.get("number")
        if number is not None:
            if isinstance(number, bool) or not re.fullmatch(r"[0-9]{1,7}", str(number)) or int(number) < 1:
                raise BotError("Поддерживаются числовые номера задач основного каталога LeetCode.")
            number = int(number)
        try:
            timestamp = int(data.get("timestamp", 0))
        except (ValueError, TypeError) as error:
            raise BotError("LeetCode вернул некорректное время решения.") from error
        difficulty = data.get("difficulty", "")
        if not isinstance(difficulty, str):
            raise BotError("LeetCode вернул некорректную сложность задачи.")
        return cls(number, title, slug, difficulty, timestamp)


@dataclass(frozen=True)
class Snapshot:
    problems: tuple[Problem, ...]
    total_solved: int
    complete: bool


class LeetCodeClient:
    def __init__(self, username: str, state_dir: Path, *, load_session: bool = True) -> None:
        self.username = parse_username(username)
        self.session_file = state_dir / "leetcode_session.json"
        self.http = requests.Session()
        self.http.headers.update({"User-Agent": "LeetCodeTableBot/2.0", "Referer": "https://leetcode.com/"})
        self.lock = threading.RLock()
        self.authenticated = False
        self.verified = False
        self.metadata: dict[str, Problem] = {}
        if load_session:
            self._load_session()

    def _load_session(self) -> None:
        if not self.session_file.exists():
            return
        try:
            data = json.loads(self.session_file.read_text(encoding="utf-8"))
            if data["username"].casefold() != self.username.casefold():
                raise BotError("Локальная сессия принадлежит другому профилю. Запустите python bot.py --login.")
            self._set_cookies(data["session"], data["csrf"])
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            raise BotError("Не удалось прочитать сессию LeetCode. Повторите вход: python bot.py --login.") from error

    def _set_cookies(self, session: str, csrf: str) -> None:
        for value in (session, csrf):
            if not isinstance(value, str) or not value or len(value) > 12000 or any(char.isspace() or char in ";\r\n" for char in value):
                raise BotError("Скопируйте только значение cookie из браузера, без имени и без Cookie:.")
        self.http.cookies.set("LEETCODE_SESSION", session, domain="leetcode.com", path="/", secure=True)
        self.http.cookies.set("csrftoken", csrf, domain="leetcode.com", path="/", secure=True)
        self.http.headers["x-csrftoken"] = csrf
        self.authenticated = True
        self.verified = False

    def login(self, session: str, csrf: str) -> None:
        with self.lock:
            self._set_cookies(session, csrf)
            self._verify_account()
            write_json(self.session_file, {"username": self.username, "session": session, "csrf": csrf})

    def _query(self, query: str, variables: dict | None = None) -> dict:
        with self.lock:
            try:
                response = self.http.post(GRAPHQL_URL, json={"query": query, "variables": variables or {}}, timeout=(10, 30), allow_redirects=False)
                response.raise_for_status()
                if response.is_redirect:
                    raise BotError("LeetCode перенаправил запрос. Проверьте доступ к сайту и повторите позже.")
                body = response.json()
            except requests.HTTPError as error:
                status = error.response.status_code if error.response is not None else "?"
                if status in (401, 403) and self.authenticated:
                    raise BotError(f"LeetCode не разрешил доступ к сессии (HTTP {status}). При необходимости повторите --login.") from error
                raise BotError(f"LeetCode не отдал историю решений (HTTP {status}). Следующая проверка через минуту.") from error
            except requests.RequestException as error:
                raise BotError("LeetCode не отвечает. Следующая проверка через минуту.") from error
            except ValueError as error:
                raise BotError("LeetCode вернул страницу вместо данных. Следующая проверка через минуту.") from error
            if not isinstance(body, dict) or body.get("errors") or not isinstance(body.get("data"), dict):
                raise BotError("LeetCode не вернул ожидаемые данные. Проверьте профиль, доступность истории и сессию.")
            return body["data"]

    def _verify_account(self) -> None:
        data = self._query("query { userStatus { username isSignedIn } }")
        user = data.get("userStatus") or {}
        if not isinstance(user, dict) or user.get("isSignedIn") is not True:
            raise BotError("Сессия LeetCode истекла или недействительна. Запустите python bot.py --login.")
        if str(user.get("username", "")).casefold() != self.username.casefold():
            raise BotError("В браузере открыт другой аккаунт LeetCode. Нужна сессия профиля из конфига.")
        self.verified = True

    def fetch_snapshot(self) -> Snapshot:
        if self.authenticated:
            return self._full_snapshot()
        data = self._query(RECENT_QUERY, {"username": self.username, "limit": 20})
        user = data.get("matchedUser")
        if not isinstance(user, dict):
            raise BotError(f"Профиль {self.username} не найден на LeetCode.")
        try:
            stats = user["submitStatsGlobal"]["acSubmissionNum"]
            total = next(item["count"] for item in stats if item["difficulty"] == "All")
            raw = data["recentAcSubmissionList"]
            if type(total) is not int or total < 0 or not isinstance(raw, list):
                raise ValueError
            unique = {}
            for item in raw:
                problem = Problem.from_data(item)
                unique[problem.slug] = max((unique.get(problem.slug, problem), problem), key=lambda task: task.timestamp)
        except (KeyError, TypeError, StopIteration, ValueError) as error:
            raise BotError("Не удалось прочитать историю принятых решений LeetCode.") from error
        problems = tuple(sorted(unique.values(), key=lambda problem: (problem.timestamp, problem.slug)))
        return Snapshot(problems, total, len(problems) >= total)

    def _full_snapshot(self) -> Snapshot:
        # Истёкшая сессия не должна превращаться в ложный «пустой список».
        self._verify_account()
        problems = {}
        skip = 0
        while True:
            data = self._query(SOLVED_QUERY, {"skip": skip, "limit": 100, "filters": {"status": "AC"}})
            result = data.get("solved")
            if not isinstance(result, dict) or type(result.get("total")) is not int or result["total"] < 0 or not isinstance(result.get("questions"), list):
                raise BotError("LeetCode не вернул полный список решённых задач.")
            total = result["total"]
            page = result["questions"]
            for item in page:
                if not isinstance(item, dict) or str(item.get("status", "")).lower() != "ac":
                    raise BotError("LeetCode не подтвердил Accepted. Проверьте сессию через --login.")
                problem = Problem.from_data(item)
                if problem.number is None:
                    raise BotError("В полном списке отсутствует номер задачи.")
                problems[problem.slug] = problem
            skip += len(page)
            if skip >= total:
                break
            if not page or skip > 100000:
                raise BotError("LeetCode вернул неполный список. Повторим сверку через минуту.")
        if len(problems) != total:
            raise BotError("Список задач изменился во время загрузки. Повторим сверку через минуту.")
        return Snapshot(tuple(sorted(problems.values(), key=lambda problem: problem.number or 0)), total, True)

    def resolve(self, problem: Problem) -> Problem:
        if problem.number is not None:
            return problem
        if problem.slug in self.metadata:
            return replace(self.metadata[problem.slug], timestamp=problem.timestamp)
        data = self._query(DETAIL_QUERY, {"slug": problem.slug})
        resolved = Problem.from_data(data.get("question"))
        if resolved.number is None or resolved.slug != problem.slug:
            raise BotError("Не удалось проверить номер задачи. Она останется в очереди до следующей проверки.")
        self.metadata[problem.slug] = resolved
        return replace(resolved, timestamp=problem.timestamp)
