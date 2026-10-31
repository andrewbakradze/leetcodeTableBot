"""Интерфейс: задача, difficulties и ожидание новых решений."""

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text

from .config import RESOLVE_OPTIONS
from .leetcode import Problem


class TerminalUI:
    def __init__(self) -> None:
        self.console = Console()

    def banner(self) -> None:
        self.console.print(Panel(
            "[bold white]LeetCode → Google Sheets[/bold white]\n"
            "[dim]Решайте задачи. Записывайте только difficulties.[/dim]",
            title="[bold cyan]SYNC JOURNAL[/bold cyan]", border_style="cyan", padding=(1, 3),
        ))

    def ask(self, label: str, default: str | None = None) -> str:
        return Prompt.ask(f"[bold cyan]{label}[/bold cyan]", console=self.console, default=default).strip()

    def ask_secret(self, label: str) -> str:
        return Prompt.ask(f"[bold cyan]{label}[/bold cyan]", console=self.console, password=True).strip()

    def error(self, message: str) -> None:
        self.console.print(Panel(Text(message), title="[red]Не получилось[/red]", border_style="red"))

    def warning(self, message: str) -> None:
        self.console.print(Text(message, style="yellow"))

    def success(self, message: str) -> None:
        self.console.print(Text(f"✓ {message}", style="green"))

    def problem(self, problem: Problem) -> None:
        text = Text(f"#{problem.number}  {problem.title}", style="bold white")
        if problem.difficulty:
            color = {"Easy": "green", "Medium": "yellow", "Hard": "red"}.get(problem.difficulty, "white")
            text.append(f"   {problem.difficulty}", style=color)
        text.append(f"\n{problem.url}", style=f"cyan link {problem.url}")
        self.console.print(Panel(text, title="Принято на LeetCode • ещё нет в таблице", border_style="cyan", padding=(1, 2)))

    def notes(self) -> str | None:
        self.console.print(
            "[bold]Какие difficulties возникли в этой задаче?[/bold]\n"
            "[dim]Несколько строк; точка на отдельной строке — сохранить.\n"
            "/skip — сохранить без заметки; /later — отложить до следующего запуска.[/dim]"
        )
        lines = []
        length = 0
        while True:
            line = self.console.input("[cyan]  │ [/cyan]")
            command = line.strip().lower()
            if command == "/later":
                return None
            if command == "/skip":
                return ""
            if command == ".":
                return "\n".join(lines).strip()
            added = len(line) + bool(lines)
            if length + added > 50000:
                self.warning("Лимит заметки — 50 000 символов. Завершите ввод точкой или /later.")
                continue
            lines.append(line)
            length += added

    def resolve_choice(self) -> str:
        self.console.print("[bold]Resolve — нужно решить эту задачу повторно?[/bold]")
        for index, option in enumerate(RESOLVE_OPTIONS, 1):
            self.console.print(f"  [cyan]{index}[/cyan] — {option}")
        answers = {str(index): option for index, option in enumerate(RESOLVE_OPTIONS, 1)}
        answers.update({option.casefold(): option for option in RESOLVE_OPTIONS})
        while True:
            value = self.ask("Выберите 1, 2 или 3").casefold()
            if value in answers:
                return answers[value]
            self.warning("Выберите: 1 — ДА, 2 — может быть, 3 — нет.")
